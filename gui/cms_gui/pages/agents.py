"""Integrations -> Agents: the agent CLIs QAVector talks to, and what they did.

An agent connection is one CLI on this machine - Claude Code - with the model it
is asked for by default. It is set once for the whole of QAVector, not for a
project: the CLI signs in once for the machine. The CLI keeps its own sign-in;
nothing typed here is a secret and nothing here is one.

Three tabs: **Chat** - the project's conversations with an agent, which lives
in ``agentchat.py`` - **Connections**, and **Activity**: every turn taken with
an agent in this project, whoever asked.

Showing the page reads files through the core and asks no model anything. An
agent's program is started only by what a person does: a message sent in Chat,
**Test** (and **Check** in the dialog), which ask it who is signed in, and
**Sign in...**, which hands it the browser.
"""

import os
import subprocess
import threading

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QPushButton, QScrollArea, QTabWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from .. import background, core as core_mod, search, theme, widgets
from ..agentmodel import ModelPicker
from .agentchat import ChatTab
from .agentskills import SkillsTab
from .agentroles import RolesTab
from .agentusage import UsageTab
from .agentbudget import BUDGET_NOTE, BudgetFields
from .agenttasks import TasksTab
from .agentwords import (KINDS, context_text, cost_text, count_text,  # noqa: F401
                         folder_text, kind_name, model_text, program_text, result_text,
                         status_text, tokens_text, tokens_total, took_text, when_text)

#: Requests that change a file. One of these is never cancelled half way; its
#: answer is dropped when the page it belonged to has moved on.
WRITES = ("save", "remove", "test", "session-new", "session-set", "session-delete",
          "skill-add", "skill-remove", "role-save", "role-delete")


def start_detached(argv):
    """Start a program of the person's own - a CLI's sign-in - and let it go.

    Its own session, so closing QAVector while a browser sign-in waits does not
    take the sign-in with it.
    """
    return subprocess.Popen(argv, start_new_session=(os.name != "nt"))


def _set_role(label, role):
    """Give a label another of the stylesheet's roles, and make it look again."""
    if label.property("role") == role:
        return
    label.setProperty("role", role)
    label.style().unpolish(label)
    label.style().polish(label)


def _table(headers):
    table = widgets.Table(0, len(headers), reclick_clears=False)
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setShowGrid(False)
    # One line a row, cut at the column's edge: wrapped, a cell breaks at its
    # first space and shows "Signed in as ..." with half the column empty.
    table.setWordWrap(False)
    header = table.horizontalHeader()
    header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    for column in range(len(headers)):
        header.setSectionResizeMode(column, QHeaderView.Interactive)
    return table


def _cell(text):
    item = QTableWidgetItem(str(text) if text not in (None, "") else "—")
    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
    return item


# -- Connections ------------------------------------------------------------------
class ConnectionsTab(QWidget):
    """The agent connections: add, change, test, sign in, remove."""

    #: (document for ``agents save``, done(payload) or None)
    save_requested = Signal(dict, object)
    #: (connection id, done(payload) or None)
    test_requested = Signal(str, object)
    sign_in_requested = Signal(str, object)
    remove_requested = Signal(str, object)
    #: (done(kinds)) - which agents the core knows and where each CLI is
    kinds_requested = Signal(object)
    #: (connection not saved yet, done(payload)) - who its CLI is signed in as
    check_requested = Signal(dict, object)
    models_requested = Signal(dict, object)

    HEADERS = ("Name", "Agent", "Program", "Defaults", "Status")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.connections, self.kinds = [], []
        self.dialog = None
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 14, 0, 0)
        column.setSpacing(8)
        column.addWidget(widgets.lede(
            "A connection is an agent's command-line program on this computer, set "
            "once for every project. The program signs in by itself and keeps its "
            "own sign-in: nothing typed here is a secret."))
        panel = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        self.table = _table(self.HEADERS)
        header = self.table.horizontalHeader()
        # The status is the longest thing a row says - an account and a date.
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        for index, width in ((0, 160), (1, 120), (2, 220), (3, 160)):
            self.table.setColumnWidth(index, width)
        self.table.itemSelectionChanged.connect(self._sync)
        self.table.itemDoubleClicked.connect(lambda _item: self.edit())
        self.search = widgets.SearchRow(self.table)
        self.search.changed.connect(self._say_empty)
        self.note = widgets.empty_note(
            self.table, "No agent connection yet - + Connection adds one.")
        self.skeleton = widgets.Skeleton(self.table, "rows")
        panel.layout().addWidget(self.table)
        column.addWidget(panel, 1)
        self.add_button = QPushButton("+ Connection")
        self.add_button.clicked.connect(self.add)
        self.edit_button = QPushButton("Edit…")
        self.edit_button.clicked.connect(lambda: self.edit())
        self.test_button = QPushButton("Test")
        self.test_button.setToolTip("Ask the program who it is signed in as, once")
        self.test_button.clicked.connect(self.test)
        self.sign_in_button = QPushButton("Sign in…")
        self.sign_in_button.setToolTip("Let the program sign in through your browser")
        self.sign_in_button.clicked.connect(self.sign_in)
        self.remove_button = QPushButton("Remove…")
        self.remove_button.clicked.connect(self.remove)
        self.message = QLabel("")
        self.message.setProperty("role", "muted")
        column.addWidget(widgets.row(self.add_button, self.edit_button, self.test_button,
                                     self.sign_in_button, self.remove_button,
                                     self.message, None))
        self._sync()

    def set_connections(self, connections):
        self.connections = list(connections or [])
        chosen = self.selected()
        chosen_id = chosen.get("id") if chosen else ""
        self.table.setRowCount(0)
        for one in self.connections:
            row = self.table.rowCount()
            self.table.insertRow(row)
            check = one.get("last_check") or {}
            values = (one.get("name"), kind_name(one.get("kind"), self.kinds),
                      program_text(one), model_text(one), status_text(one))
            for column, value in enumerate(values):
                item = _cell(value)
                if column == 0:
                    item.setData(Qt.UserRole, one.get("id"))
                    item.setToolTip("id: %s" % one.get("id"))
                if column == 4 and check and not check.get("ok"):
                    item.setForeground(QColor(theme.BAD_INK))
                    item.setToolTip(str(check.get("error") or ""))
                self.table.setItem(row, column, item)
            if one.get("id") == chosen_id:
                self.table.selectRow(row)
        self.search.apply()
        self._sync()

    def select(self, connection_id):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.data(Qt.UserRole) == connection_id:
                self.table.selectRow(row)
                return True
        return False

    def selected(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        wanted = item.data(Qt.UserRole) if item is not None else ""
        return next((one for one in self.connections if one.get("id") == wanted), None)

    def _sync(self):
        chosen = self.selected() is not None
        for button in (self.edit_button, self.test_button, self.sign_in_button,
                       self.remove_button):
            button.setEnabled(chosen)

    def _say_empty(self):
        self.note.say_filtered("No connection matches the search."
                               if self.search.searching() else "")

    def say(self, text):
        self.message.setText(text or "")

    # -- doing ----------------------------------------------------------------
    def add(self):
        self._open(None)

    def edit(self, connection=None):
        connection = connection or self.selected()
        if connection is not None:
            self._open(connection)

    def _open(self, connection):
        dialog = ConnectionDialog(self, connection,
                                  taken=[one.get("name", "") for one in self.connections
                                         if one is not connection],
                                  kinds=self.kinds,
                                  others=[one for one in self.connections if one is not connection])
        dialog.save_requested.connect(self.save_requested)
        dialog.check_requested.connect(self.check_requested)
        dialog.models_requested.connect(self.models_requested)
        self.dialog = dialog

        def heard(kinds):
            self.kinds = list(kinds or [])
            if self.dialog is dialog:
                dialog.set_kinds(self.kinds)

        # Where each CLI is on this machine: a look along PATH, asked when the
        # dialog opens because that is when somebody needs to know.
        self.kinds_requested.emit(heard)
        dialog.exec()
        self.dialog = None
        dialog.deleteLater()

    def test(self):
        chosen = self.selected()
        if chosen is None:
            return
        self.test_button.setEnabled(False)
        self.say("Asking who is signed in…")

        def done(payload):
            self._sync()
            if payload.get("ok"):
                self.say((payload.get("result") or {}).get("summary") or "Signed in.")
            else:
                self.say(payload.get("error") or "The check failed.")

        self.test_requested.emit(chosen["id"], done)

    def sign_in(self):
        chosen = self.selected()
        if chosen is None:
            return
        self.sign_in_button.setEnabled(False)

        def done(payload):
            self._sync()
            argv = payload.get("argv") or []
            if not payload.get("ok") or not argv:
                self.say(payload.get("error") or "Cannot sign in from here.")
                return
            try:
                start_detached(argv)
            except OSError as exc:
                self.say("Could not start it: %s" % exc)
                return
            self.say("%s Press Test when the browser says you are signed in."
                     % (payload.get("detail") or payload.get("summary") or ""))

        self.sign_in_requested.emit(chosen["id"], done)

    def remove(self):
        chosen = self.selected()
        if chosen is None:
            return
        detail = ("Whatever names this connection stops working until it names "
                  "another. Conversations already held with it stay readable. "
                  "The program itself, and its sign-in, are left alone.")
        if not widgets.confirm(self, "Remove connection",
                               "Remove %s?" % chosen.get("name"), detail,
                               agree="Remove", kind="warn"):
            return

        def done(payload):
            self.say("" if payload.get("ok") else payload.get("error") or "Not removed.")

        self.remove_requested.emit(chosen["id"], done)


#: How its conversations go on when what they handed on ends (ADR-009 D3).
WAKE_DEFAULT = (("Ask - a card waits for you", "ask"), ("Auto - go on by itself", "continue"),
                ("Off - only show the results", "off"))

#: The "QAVector access" switches, in the order the form shows them.
QAVECTOR_ACCESS = (("services", "Start, stop and restart services"),
                   ("scenarios", "Run scenarios"),
                   ("cycles", "Run cycles"),
                   ("approvals", "Answer a cycle's approvals and questions"),
                   ("jira", "Read Jira"),
                   ("jira_write", "Change Jira: comment, move issues - you approve each"),
                   ("teams", "Post to Teams - as far as each channel allows"),
                   ("usage", "Read agent usage and est. cost"))


#: What a connection's conversations start with for other agents (ADR-006 D3).
OTHERS_DEFAULT = (("Ask - each one waits for your yes", "approve"), ("Auto - without asking", "auto"),
                  ("Off - work alone", "deny"))


class ConnectionDialog(QDialog):
    """Add an agent connection, or change one.

    Everything in it is optional but the name: a blank program is found automatically
    each time it is used, and a blank model or effort is the CLI's own choice.
    **Check** asks the program who is signed in before anything is saved.
    """

    save_requested = Signal(dict, object)
    #: (connection as the form has it, done(payload))
    check_requested = Signal(dict, object)
    models_requested = Signal(dict, object)

    def __init__(self, parent, connection=None, taken=(), kinds=(), others=()):
        super().__init__(parent)
        self.connection = dict(connection or {})
        #: The other connections, the ones its conversations may work with.
        self._others = [dict(one) for one in others or [] if one.get("id") != self.connection.get("id")]
        #: Each provider's name as the core says it, for the other connections' boxes.
        self._kind_names = dict((value, name) for name, value in KINDS)
        self._kind_names.update({one["kind"]: one.get("name") or one["kind"] for one in kinds or [] if one.get("kind")})
        self._taken = {name.casefold() for name in taken if name}
        self._kinds = []
        self.setWindowTitle("Edit connection" if connection else "Add an agent connection")
        self.frame = widgets.dress(self)
        self.setMinimumWidth(480)
        outer = QVBoxLayout(self)
        outer.setSpacing(8)
        # The fields scroll; what is done with them stays at the bottom. All of
        # it no longer fits a laptop's screen at once.
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        column = QVBoxLayout(body)
        column.setContentsMargins(0, 0, 4, 0)
        column.setSpacing(8)
        self.scroll.setWidget(body)
        outer.addWidget(self.scroll, 1)
        self.name = QLineEdit(self.connection.get("name", ""))
        self.name.setPlaceholderText("Agent connection")
        column.addWidget(widgets.field(
            "Name", self.name,
            "Whatever uses it names it by its id%s, which never changes when you "
            "rename it." % (" (%s)" % self.connection["id"]
                            if self.connection.get("id") else "")))
        self.kind = QComboBox()
        # Fixed once it is made: conversations held with one agent's program
        # cannot be carried on by another's.
        self.kind.setEnabled(not connection)
        column.addWidget(widgets.field("Agent", self.kind))
        self.binary = QLineEdit(self.connection.get("binary", ""))
        self.binary.setProperty("mono", "true")
        self.browse = QPushButton("Browse…")
        self.browse.setProperty("variant", "ghost")
        self.browse.clicked.connect(self._browse)
        self.found = QLabel("")
        self.found.setProperty("role", "hint")
        self.found.setWordWrap(True)
        program = widgets.field("Program", widgets.row(self.binary, self.browse))
        program.layout().addWidget(self.found)
        column.addWidget(program)
        self.model = ModelPicker()
        self.model.set_value(self.connection.get("model", ""))
        self.model.requested.connect(lambda done: self.models_requested.emit(self.entry(), done))
        self.binary.textChanged.connect(self._model_context)
        column.addWidget(widgets.field(
            "Default model", self.model,
            "What a conversation or a cycle step that chooses no model is run with. "
            "Open the list to read the models this program offers - no prompt is sent."))
        self.effort = QComboBox()
        column.addWidget(widgets.field(
            "Default effort", self.effort,
            "How hard the model is asked to think when a conversation or a step "
            "does not say. The program's own choice leaves it to its settings."))
        self.role = QLineEdit(self.connection.get("role", ""))
        self.role.setObjectName("agentRole")
        self.role.setMaxLength(500)
        self.role.setPlaceholderText("e.g. implementation and tests; careful reviews")
        column.addWidget(widgets.field(
            "Good at", self.role,
            "What other agents are told this one is good at, when they may hand it a task. "
            "They also choose its model and effort for the task."))
        # What this agent may do in QAVector itself, through QCR (ADR-003 D6).
        # Every switch off until somebody turns it on; a turn gets exactly these.
        access = self.connection.get("qavector_access") or {}
        self.access = {}
        rows = QVBoxLayout()
        rows.setSpacing(4)
        for key, label in QAVECTOR_ACCESS:
            box = QCheckBox(label)
            box.setChecked(access.get(key) is True)
            self.access[key] = box
            rows.addWidget(box)
        holder = QWidget()
        holder.setLayout(rows)
        rows.setContentsMargins(0, 0, 0, 0)
        column.addWidget(widgets.field(
            "QAVector access", holder,
            "What this agent's conversations may do here, through “Run service actions through QCR”. "
            "Everything it does is journalled (Log, Executions mode). Approvals let it answer a cycle's "
            "gates and questions in your place."))
        column.addWidget(self._others_field())
        # How its conversations go on when what they handed on ends (ADR-009 D3).
        self.wake_default = QComboBox()
        self.wake_default.setObjectName("agentWakeDefault")
        for label, value in WAKE_DEFAULT:
            self.wake_default.addItem(label, value)
        self.wake_default.setCurrentIndex(max(0, self.wake_default.findData(
            self.connection.get("wake_default") or "ask")))
        column.addWidget(widgets.field(
            "When delegated tasks finish", widgets.row(self.wake_default, None),
            "The next step when the tasks a conversation handed on are done. Auto: it goes on by itself "
            "with their results. Ask: a card waits for you. Each conversation can change it (Steps)."))
        # What a task it hands on, and one of its conversations, may cost before
        # you are asked (ADR-013). None unless set.
        self.budget_task = BudgetFields(self.connection.get("budget_task"))
        self.budget_task.setObjectName("agentBudgetTask")
        self.budget_conversation = BudgetFields(self.connection.get("budget_conversation"))
        self.budget_conversation.setObjectName("agentBudgetConversation")
        column.addWidget(widgets.field("Budget per task", self.budget_task,
                                       "What one task this agent hands on may cost. " + BUDGET_NOTE))
        column.addWidget(widgets.field("Budget per conversation", self.budget_conversation,
                                       "What one conversation may cost, with what it hands on. Each conversation "
                                       "can change its own in Settings."))
        self.check = QPushButton("Check")
        self.check.setToolTip("Ask the program who it is signed in as. Nothing is saved.")
        self.check.clicked.connect(self._check)
        self.state = QLabel("")
        self.state.setProperty("role", "hint")
        self.state.setWordWrap(True)
        column.addWidget(widgets.field("Sign-in", widgets.row(self.check, self.state,
                                                              stretch_last=True)))
        self.problem = QLabel("")
        self.problem.setProperty("role", "error")
        self.problem.setWordWrap(True)
        self.problem.setVisible(False)
        outer.addWidget(self.problem)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.save = QPushButton("Save" if connection else "Add")
        self.save.setProperty("variant", "primary")
        self.save.setDefault(True)
        self.save.clicked.connect(self._save)
        buttons.addWidget(self.save)
        outer.addLayout(buttons)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self.set_kinds(kinds)
        widgets.see_through(body)
        self.scroll.viewport().setProperty("seethrough", "true")
        # As tall as the fields want, up to most of the screen; then it scrolls.
        screen = self.screen().availableGeometry() if self.screen() is not None else None
        wanted = body.sizeHint().height() + 110
        self.resize(max(self.minimumWidth(), self.sizeHint().width()),
                    min(wanted, int(screen.height() * 0.88)) if screen is not None else wanted)

    # -- which agents there are ------------------------------------------------
    def set_kinds(self, kinds):
        """What the core knows of each agent's program: called again when the
        answer comes, with whatever was typed meanwhile left as it is."""
        self._kinds = [dict(one) for one in kinds or []] or [
            {"kind": value, "name": name} for name, value in KINDS]
        # A connection that exists keeps its agent, whatever list is shown: the
        # field is fixed for it, and the list may not even name it yet (the core's
        # answer comes after the dialog opens). Saving another kind once turned a
        # Codex connection into a Claude one.
        own = self.connection.get("kind") if self.connection.get("id") else ""
        wanted = own or self.kind.currentData() or self._kinds[0]["kind"]
        if own and not any(one.get("kind") == own for one in self._kinds):
            self._kinds.append({"kind": own, "name": own})
        self.kind.blockSignals(True)
        self.kind.clear()
        for one in self._kinds:
            self.kind.addItem(one.get("name") or one["kind"], one["kind"])
        self.kind.setCurrentIndex(max(0, self.kind.findData(wanted)))
        self.kind.blockSignals(False)
        self._kind_changed()

    def _kind(self):
        chosen = self.kind.currentData()
        return next((one for one in self._kinds if one["kind"] == chosen), self._kinds[0])

    def _kind_changed(self, *_args):
        kind = self._kind()
        name = kind.get("name") or kind["kind"]
        wanted = self.effort.currentData()
        if wanted is None:
            wanted = self.connection.get("effort", "")
        self.effort.clear()
        self.effort.addItem("The program's own choice", "")
        for level in kind.get("efforts") or ([wanted] if wanted else []):
            self.effort.addItem(level, level)
        self.effort.setCurrentIndex(max(0, self.effort.findData(wanted)))
        default = kind.get("default_binary") or kind["kind"]
        self.binary.setPlaceholderText("%s, found automatically" % default)
        where = kind.get("found_where") or "PATH"
        if "found" not in kind:
            self.found.setText("Blank finds %s on PATH each time it is used." % default)
        elif kind["found"]:
            self.found.setText("Blank uses %s, found %s." % (
                kind["found"], "on PATH" if where == "PATH" else "in %s" % where))
        else:
            self.found.setText("%s is not installed on this computer. %s Or give the path "
                               "of its program." % (name, kind.get("install_hint")
                                                    or "Install it, then check again."))
        if not self.connection and not self.name.text().strip() \
                and name.casefold() not in self._taken:
            # A first connection needs no thought: it is called what it is.
            self.name.setText(name)
            self.name.selectAll()
        self._model_context()

    def _model_context(self, *_args):
        name = self._kind().get("name") or "the program"
        self.model.set_context((self.kind.currentData(), self.binary.text().strip()),
                               self._kind().get("model_default_label") or "Use %s settings" % name)

    def _browse(self):
        chosen = widgets.pick_path(self, "The agent's program",
                                   self.binary.text().strip() or "~")
        if chosen:
            self.binary.setText(chosen)

    # -- asking and saving -----------------------------------------------------
    def _others_field(self):
        """Other agents (ADR-006 D3): what its conversations start with, and
        which other connections they may hand tasks to and message. A new
        connection asks each time and may reach every connection already here."""
        new = not self.connection.get("id")
        self.others_default = QComboBox()
        for label, value in OTHERS_DEFAULT:
            self.others_default.addItem(label, value)
        wanted = "approve" if new else (self.connection.get("others_default") or "deny")
        self.others_default.setCurrentIndex(max(0, self.others_default.findData(wanted)))
        allowed = set(self.connection.get("allowed_agents") or [])
        self.allowed = {}
        boxes = QHBoxLayout()
        boxes.setContentsMargins(0, 0, 0, 0)
        boxes.setSpacing(12)
        for one in self._others:
            kind = self._kind_names.get(one.get("kind"), one.get("kind") or "")
            box = QCheckBox("%s (%s)" % (one.get("name") or one["id"], kind) if kind else one.get("name") or one["id"])
            box.setChecked(new or one["id"] in allowed)
            self.allowed[one["id"]] = box
            boxes.addWidget(box)
        if not self._others:
            nobody = QLabel("No other agent connection yet.")
            nobody.setProperty("role", "hint")
            boxes.addWidget(nobody)
        boxes.addStretch(1)
        self.others_default.currentIndexChanged.connect(self._sync_others)
        self._sync_others()
        holder = QWidget()
        stack = QVBoxLayout(holder)
        stack.setContentsMargins(0, 0, 0, 0)
        stack.setSpacing(4)
        stack.addWidget(widgets.row(self.others_default, None))
        stack.addLayout(boxes)
        return widgets.field(
            "Delegation mode", holder,
            "Its conversations may hand tasks to, and message, the agents ticked here - each "
            "conversation can change the mode for itself. Ask shows you every one first; Off keeps it "
            "to its own provider.")

    def _sync_others(self, *_args):
        working = self.others_default.currentData() != "deny"
        for box in self.allowed.values():
            box.setEnabled(working)

    def entry(self):
        entry = {key: value for key, value in self.connection.items() if key != "last_check"}
        entry.update({"name": self.name.text().strip(), "kind": self.kind.currentData(),
                      "binary": self.binary.text().strip(),
                      "model": self.model.value(),
                      "effort": self.effort.currentData() or "",
                      "role": self.role.text().strip(),
                      "qavector_access": {key: box.isChecked() for key, box in self.access.items()},
                      "others_default": self.others_default.currentData(),
                      "wake_default": self.wake_default.currentData(),
                      "budget_task": self.budget_task.budget(),
                      "budget_conversation": self.budget_conversation.budget(),
                      "allowed_agents": [key for key, box in self.allowed.items() if box.isChecked()]})
        return entry

    def _check(self):
        self.check.setEnabled(False)
        _set_role(self.state, "hint")
        self.state.setText("Asking who is signed in…")

        def done(payload):
            self.check.setEnabled(True)
            if payload.get("ok"):
                _set_role(self.state, "hint")
                self.state.setText((payload.get("result") or {}).get("summary")
                                   or "Signed in.")
            else:
                _set_role(self.state, "error")
                self.state.setText(payload.get("error") or "The check failed.")

        self.check_requested.emit(self.entry(), done)

    def _save(self):
        entry = self.entry()
        if not entry["name"]:
            self._say(["Name is required."])
            return
        if entry["name"].casefold() in self._taken:
            self._say(["A connection called %r already exists." % entry["name"]])
            return
        self.save.setEnabled(False)
        self.save.setText("Saving…")

        def done(payload):
            self.save.setEnabled(True)
            self.save.setText("Save" if self.connection.get("id") else "Add")
            if payload.get("ok"):
                self.accept()
            else:
                self._say(payload.get("problems") or [payload.get("error") or "Not saved."])

        self.save_requested.emit({"connection": entry}, done)

    def _say(self, problems):
        self.problem.setText("\n".join(one for one in problems if one))
        self.problem.setVisible(any(problems))


# -- Activity -----------------------------------------------------------------------
class ActivityTab(QWidget):
    """Every turn taken with an agent in this project, the newest first."""

    HEADERS = ("When", "Connection", "From", "Folder", "Mode", "Model", "Skills", "Took",
               "Context", "Est. cost", "Result")
    SKILLS = 6
    CONTEXT = 8
    RESULT = 10

    #: The tab wants its rows: the project's own record, never an agent.
    wanted = Signal()
    #: (session id) - somebody wants to read the conversation a turn is part of.
    session_wanted = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 14, 0, 0)
        column.setSpacing(8)
        column.addWidget(widgets.lede(
            "Every turn taken with an agent in this project, whoever asked: where "
            "it ran, what it was allowed, how long it took and how it ended."))
        panel = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        self.table = _table(self.HEADERS)
        self.table.horizontalHeader().setSectionResizeMode(self.RESULT, QHeaderView.Stretch)
        for index, width in ((0, 134), (1, 100), (2, 124), (3, 126), (4, 52), (5, 84),
                             (6, 58), (7, 74), (8, 96), (9, 64)):
            self.table.setColumnWidth(index, width)
        self.table.itemDoubleClicked.connect(self._open)
        self.search = widgets.SearchRow(self.table)
        self.search.changed.connect(self._say_empty)
        self.note = widgets.empty_note(
            self.table, "No agent has been asked anything in this project yet. Every "
                        "turn taken with one will be listed here.")
        self.skeleton = widgets.Skeleton(self.table, "rows")
        panel.layout().addWidget(self.table)
        column.addWidget(panel, 1)
        self.message = QLabel("")
        self.message.setProperty("role", "muted")
        column.addWidget(self.message)

    def shown(self):
        self.wanted.emit()

    def say(self, text):
        self.message.setText(text or "")

    def set_rows(self, rows):
        """The log's entries, newest first, as the table says them."""
        self.rows = list(rows or [])
        self.table.setRowCount(0)
        for one in self.rows:
            row = self.table.rowCount()
            self.table.insertRow(row)
            # Said by whoever asked; this page shows it and reads nothing into it.
            source = one.get("source") or {}
            done = one.get("state") not in ("running", None, "")
            skills = [str(name) for name in one.get("skills") or []]
            tokens = one.get("tokens") or {}
            values = (when_text(one.get("started")),
                      one.get("connection_name") or one.get("connection"),
                      source.get("label"), folder_text(one.get("folder")),
                      "plan" if one.get("plan") else one.get("mode"),
                      one.get("model") or "CLI default",
                      # How many it was handed; which ones is the cell's tooltip.
                      str(len(skills)) if skills else "",
                      took_text(one.get("started"), one.get("ended")) if done else "",
                      # How full the agent's context was; the tokens are its tooltip.
                      context_text(one.get("context")),
                      cost_text(one.get("cost_usd")), result_text(one))
            for column, value in enumerate(values):
                item = _cell(value)
                if column == 0:
                    item.setData(Qt.UserRole, one.get("session") or "")
                    item.setToolTip("%s\nDouble-click to read the conversation."
                                    % (one.get("title") or "Untitled"))
                if column == 2 and source.get("detail"):
                    item.setToolTip(str(source["detail"]))
                if column == 3:
                    item.setToolTip(str(one.get("folder") or ""))
                if column == self.SKILLS and skills:
                    item.setToolTip(", ".join(skills))
                if column == self.CONTEXT and tokens:
                    item.setToolTip("Tokens: %s in all - %s"
                                    % (count_text(tokens_total(tokens)), tokens_text(tokens)))
                if column == self.RESULT:
                    if one.get("state") in ("failed", "interrupted"):
                        item.setForeground(QColor(theme.BAD_INK))
                    item.setToolTip(str(one.get("error") or ""))
                self.table.setItem(row, column, item)
        self.search.apply()

    def _open(self, item):
        first = self.table.item(item.row(), 0)
        session = first.data(Qt.UserRole) if first is not None else ""
        if session:
            self.session_wanted.emit(session)

    def _say_empty(self):
        self.note.say_filtered("Nothing matches the search." if self.search.searching()
                               else "")


# -- the view ------------------------------------------------------------------------
class AgentsView(QWidget):
    """Agents under Integrations: the header and its tabs."""

    back_requested = Signal()
    connections_changed = Signal()
    #: A role was added, changed or removed: what agents may hand work to (ADR-010).
    roles_changed = Signal()
    connections_loaded = Signal(dict)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.core = None
        self.connections = []
        self._asked = {}
        self._listed = False
        self._listing = False
        self._closing = False
        self._pending = {}              # cancellation event -> request kind

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 18, 24, 14)
        outer.setSpacing(4)
        crumb = QHBoxLayout()
        self.crumbs = widgets.Crumbs("Integrations")
        self.crumb = self.crumbs.root
        self.crumb.clicked.connect(self.back_requested)
        crumb.addWidget(self.crumbs)
        crumb.addStretch(1)
        self.window_actions = crumb
        outer.addLayout(crumb)
        header = QHBoxLayout()
        header.setSpacing(10)
        header.addWidget(widgets.heading("Agents"))
        header.addStretch(1)
        outer.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("agentsTabs")
        self.chat = ChatTab(settings)
        self.chat.host = self
        self.quick_composer = None
        self.connections_tab = ConnectionsTab()
        self.skills_tab = SkillsTab()
        self.activity = ActivityTab()
        self.usage = UsageTab()
        self.roles_tab = RolesTab()
        self.tasks = TasksTab()
        self.tasks.open_conversation.connect(self.open_session)
        # What the person does on the board goes through the chat: its cards say the same.
        self.tasks.detail.acts = self.chat
        self.tabs.addTab(self.chat, "Chat")
        self.tabs.addTab(self.tasks, "Tasks")
        self.tabs.addTab(self.connections_tab, "Connections")
        self.tabs.addTab(self.roles_tab, "Roles")
        self.tabs.addTab(self.skills_tab, "Skills")
        self.tabs.addTab(self.activity, "Activity")
        self.tabs.addTab(self.usage, "Usage")
        self.tabs.currentChanged.connect(self._tab_changed)
        outer.addWidget(self.tabs, 1)

        self.chat.connections_wanted.connect(self._add_first_connection)
        self.chat.skills_wanted.connect(self._add_a_skill)
        self.activity.session_wanted.connect(self.open_session)
        self.skills_tab.wanted.connect(self.load_skills)
        self.skills_tab.look_requested.connect(self.look_at_skill)
        self.skills_tab.add_requested.connect(self.add_skills)
        self.skills_tab.update_requested.connect(self.look_at_skill_again)
        self.skills_tab.remove_requested.connect(self.remove_skill)

        self.connections_tab.save_requested.connect(self.save_connection)
        self.connections_tab.test_requested.connect(self.test_connection)
        self.connections_tab.sign_in_requested.connect(self.sign_in)
        self.connections_tab.remove_requested.connect(self.remove_connection)
        self.connections_tab.kinds_requested.connect(self.load_kinds)
        self.connections_tab.models_requested.connect(self.load_models)
        self.connections_tab.check_requested.connect(self.check_connection)
        self.activity.wanted.connect(self.load_activity)
        self.usage.wanted.connect(self.load_usage)
        self.roles_tab.wanted.connect(self.load_roles)
        self.roles_tab.save_requested.connect(self.save_role)
        self.roles_tab.delete_requested.connect(self.delete_role)
        self.usage.turns_wanted.connect(self._turns_of)

    # -- the outside ------------------------------------------------------------
    def set_core(self, core):
        """A core for the project in use. The connections are the same in every
        project, but what the core is told about paths may not be - and the
        activity is the project's own - so everything is read again."""
        if core is self.core:
            return
        self._cancel_requests()
        self.core = core
        self._listed = False
        self._listing = False
        self.connections_tab.skeleton.stop()
        self.activity.skeleton.stop()
        self.connections_tab.say("")
        self.activity.say("")
        self.activity.set_rows([])
        self.usage.skeleton.stop()
        self.usage.say("")
        self.usage.set_usage(None)
        self.chat.reset()
        if self.quick_composer is not None:
            self.quick_composer.reset()
        if self.isVisible():
            self.list_connections()
            self._tab_changed(0)

    def has_unsaved(self):
        """A message typed and not sent is the one thing leaving would lose."""
        return self.chat.has_draft() or (self.quick_composer is not None
                                         and self.quick_composer.has_draft())

    def busy_reason(self, include_dialogs=True):
        if self.chat.running():
            return "An agent is still answering. Stop it, or wait for it to finish."
        if any(kind in WRITES for kind in self._pending.values()):
            return "A change in Agents is being saved. Wait for it to finish."
        if include_dialogs and any(dialog.isVisible() for dialog in self.findChildren(QDialog)):
            return "Finish or cancel the open Agents dialog first."
        return ""

    def _cancel_requests(self):
        """Stop the reads in flight. A write runs to its end, and its answer is
        dropped when the page it belonged to has moved on."""
        for cancel, kind in list(self._pending.items()):
            if kind not in WRITES:
                cancel.set()
                del self._pending[cancel]
        self._asked.clear()

    def shutdown(self):
        self._closing = True
        if self.quick_composer is not None:
            self.quick_composer.hide()
        self._cancel_requests()
        self.chat.shutdown()
        for dialog in self.findChildren(QDialog):
            dialog.reject()

    def showEvent(self, event):
        super().showEvent(event)
        if self._closing:
            return
        if not self._listed:
            self.list_connections()
        self._tab_changed(0)

    def _add_first_connection(self):
        """From the Chat tab with nobody to ask: to where one is made."""
        self.tabs.setCurrentWidget(self.connections_tab)
        self.connections_tab.add()

    def _add_a_skill(self):
        """From the composer's list, lacking the skill wanted: to the library."""
        self.tabs.setCurrentWidget(self.skills_tab)
        self.skills_tab.add()

    def open_session(self, session_id):
        """Show one conversation - asked for from the log, say."""
        self.tabs.setCurrentWidget(self.chat)
        self.chat.open_session(session_id)

    def delegate(self, chosen, source, message, done=None):
        """Open a task's conversation with a draft for the person to send."""
        self.tabs.setCurrentWidget(self.chat)
        return self.chat.delegate(chosen, source, message, done, send=False)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.StyleChange, QEvent.PaletteChange):
            # The cells that say something went wrong carry the theme's ink.
            self.connections_tab.set_connections(self.connections)
            self.activity.set_rows(self.activity.rows)
            self.skills_tab.set_skills(self.skills_tab.skills, self.skills_tab.own)

    def _tab_changed(self, _index):
        """The tab in front reads what it shows: files, through the core."""
        if self._closing or not self.isVisible():
            return
        if self.tabs.currentWidget() is self.activity:
            self.load_activity()
        elif self.tabs.currentWidget() is self.usage:
            self.usage.shown()
        elif self.tabs.currentWidget() is self.tasks:
            self.tasks.shown()
        elif self.tabs.currentWidget() is self.roles_tab:
            self.roles_tab.shown()
        elif self.tabs.currentWidget() is self.chat:
            self.chat.shown()
        elif self.tabs.currentWidget() is self.skills_tab:
            self.load_skills()

    def ask(self, kind, work, done):
        """Ask the core for a tab of this view - see :meth:`_call`."""
        self._call(kind, work, done)

    # -- search (Ctrl+K) --------------------------------------------------------
    # What is loaded and nothing more: the connections, the conversations the
    # Chat tab has listed, the skills the Skills tab has read. No program is
    # started and no model asked; there is no "search the agents" row.
    def search_rows(self):
        found = []
        for one in self.connections:
            found.append(search.hit(
                one.get("name") or one.get("id"),
                "Agent connection · %s" % program_text(one), "",
                keys=" ".join(part for part in (one.get("id"), one.get("model")) if part),
                target=("connection", one.get("id"))))
        for one in self.chat.sessions:
            source = (one.get("source") or {}).get("label") or ""
            found.append(search.hit(
                one.get("title") or "Untitled",
                " · ".join(part for part in ("Conversation", one.get("connection_name"),
                                             folder_text(one.get("folder")),
                                             "" if source == "Chat" else source) if part),
                "", keys=" ".join(part for part in (one.get("folder"), source) if part),
                target=("session", one.get("id"))))
        for one in self.skills_tab.skills:
            found.append(search.hit(one.get("name"), "Skill · %s" % (one.get("description")
                                                                   or "no description"),
                                    "", target=("skill", one.get("name"))))
        return found

    def search_source(self):
        return None

    def show_found(self, target):
        if not (isinstance(target, tuple) and len(target) == 2):
            return False
        kind, value = target
        if kind == "connection" and any(one.get("id") == value for one in self.connections):
            self.tabs.setCurrentWidget(self.connections_tab)
            self.connections_tab.select(value)
            return True
        if kind == "session" and any(one.get("id") == value for one in self.chat.sessions):
            self.open_session(value)
            return True
        if kind == "skill" and any(one.get("name") == value for one in self.skills_tab.skills):
            self.tabs.setCurrentWidget(self.skills_tab)
            self.skills_tab.select(value)
            return True
        return False

    # -- asking the core --------------------------------------------------------
    def _call(self, kind, work, done, cancel=None):
        """``work(core, cancel)`` on a thread; the latest answer reaches ``done``.

        A core that could not answer at all is an answer too - ``ok`` false,
        with why - so every caller shows a failure one way.
        """
        if self._closing or self.core is None:
            return
        self._asked[kind] = self._asked.get(kind, 0) + 1
        mine = self._asked[kind]
        core = self.core
        cancel = cancel if cancel is not None else threading.Event()
        self._pending[cancel] = kind
        handed = None if kind in WRITES else cancel

        def answer(result, error):
            self._pending.pop(cancel, None)
            if self._closing or self._asked.get(kind) != mine or core is not self.core:
                return
            if error is not None:
                if not isinstance(error, (core_mod.CoreError, AttributeError)):
                    raise error
                done({"ok": False, "kind": "error", "error": str(error)})
                return
            done(result or {})

        def request():
            if handed is not None and handed.is_set():
                return {"ok": False, "kind": "cancelled"}
            return work(core, handed)

        if background.offloads(core):
            background.run(request, answer, owner=self)
        else:
            answer(*background.call(request))

    def list_connections(self, then=None):
        """The agent connections - a file the core reads; no program is started."""
        if self.core is None or self._listing:
            return
        self._listing = True
        self.connections_tab.skeleton.start()

        def done(payload):
            self._listing = False
            self._listed = True
            self.connections_tab.skeleton.stop()
            if payload.get("ok"):
                self.connections_tab.say("")
                self._set_connections(payload.get("connections") or [])
            else:
                self.connections_tab.say(payload.get("error")
                                         or "The connections could not be read.")
            self.connections_loaded.emit(payload)
            if then is not None:
                then()

        self._call("list", lambda core, cancel: core.agents("list", cancel=cancel), done)

    def last_kind(self, done):
        """``done(kind)``: the program the most recent conversation was with
        (``claude``, ``codex``), or "" when there is none. The chat's own list
        when it has read one; else the project's conversations, read by the
        core - no agent is asked anything."""
        if self.chat._loaded:
            newest = self.chat.sessions[0] if self.chat.sessions else {}
            done(str(newest.get("kind") or ""))
            return

        def answered(payload):
            rows = (payload.get("sessions") or []) if payload.get("ok") else []
            done(str(rows[0].get("kind") or "") if rows else "")

        self._call("last-kind", lambda core, cancel: core.agents(
            "sessions", document={"limit": 1}, cancel=cancel), answered)

    def _set_connections(self, connections):
        self.connections = list(connections)
        self.connections_tab.set_connections(self.connections)
        self.chat.set_connections(self.connections)
        self.connections_changed.emit()

    def load_kinds(self, done):
        """Which agents the core knows, and where each program is found."""
        def answered(payload):
            done(payload.get("kinds") or [] if payload.get("ok") else [])

        self._call("kinds", lambda core, cancel: core.agents("kinds", cancel=cancel), answered)

    def load_models(self, connection, done):
        self._call("connection-models", lambda core, cancel: core.agents(
            "models", document={"connection": dict(connection)}, timeout=30, cancel=cancel), done)

    def save_connection(self, document, done=None):
        """Add or change a connection; ``done(payload)`` hears the answer."""
        def answered(payload):
            if payload.get("ok"):
                saved = payload.get("connection") or {}
                others = [one for one in self.connections if one.get("id") != saved.get("id")]
                place = next((index for index, one in enumerate(self.connections)
                              if one.get("id") == saved.get("id")), len(others))
                others.insert(place, saved)
                self._set_connections(others)
                self.connections_tab.select(saved.get("id"))
            if done is not None:
                done(payload)

        self._call("save", lambda core, cancel: core.agents("save", document=document),
                   answered)

    def test_connection(self, connection_id, done=None):
        """Ask a saved connection's program who is signed in; the core remembers
        the answer on the connection, and so does the table."""
        def answered(payload):
            if payload.get("last_check"):
                for one in self.connections:
                    if one.get("id") == connection_id:
                        one["last_check"] = payload["last_check"]
                self.connections_tab.set_connections(self.connections)
            if done is not None:
                done(payload)

        self._call("test", lambda core, cancel: core.agents("test", connection_id), answered)

    def check_connection(self, entry, done):
        """The same question for a connection that is still a form."""
        self._call("check", lambda core, cancel: core.agents(
            "test", document={"connection": dict(entry)}, cancel=cancel), done)

    def sign_in(self, connection_id, done):
        """The command that signs the program in. Handed back, not run: the
        page starts it, as the person's own program."""
        self._call("sign-in", lambda core, cancel: core.agents(
            "sign-in", connection_id, cancel=cancel), done)

    def remove_connection(self, connection_id, done=None):
        def answered(payload):
            if payload.get("ok"):
                self._set_connections([one for one in self.connections
                                       if one.get("id") != connection_id])
            if done is not None:
                done(payload)

        self._call("remove", lambda core, cancel: core.agents("delete", connection_id),
                   answered)

    def load_activity(self):
        """The project's record of turns - files the core reads; no agent is asked."""
        if self.core is None:
            return
        self.activity.skeleton.start()

        def done(payload):
            self.activity.skeleton.stop()
            if payload.get("ok"):
                self.activity.say("")
                self.activity.set_rows(payload.get("activity") or [])
            else:
                self.activity.say(payload.get("error") or "The activity could not be read.")

        self._call("activity", lambda core, cancel: core.agents("activity", cancel=cancel),
                   done)

    # -- roles (ADR-010) ------------------------------------------------------------
    def load_roles(self):
        """The roles agents may hand work to - the core's file; no agent is asked."""
        if self.core is None:
            return

        def done(payload):
            if payload.get("ok"):
                self.roles_tab.say("")
                self.roles_tab.set_roles(payload.get("roles") or [], payload.get("templates") or [],
                                         self.connections, self.connections_tab.kinds)
            else:
                self.roles_tab.say(payload.get("error") or "The roles could not be read.")

        self._call("roles", lambda core, cancel: core.agents("roles", cancel=cancel), done)

    def save_role(self, role, new, done):
        def saved(payload):
            done(payload)
            if payload.get("ok"):
                self.roles_changed.emit()

        self._call("role-save", lambda core, cancel: core.agents(
            "role-save", document={"role": role, "new": new}, cancel=cancel), saved)

    def delete_role(self, role_id, done):
        def removed(payload):
            done(payload)
            if payload.get("ok"):
                self.roles_changed.emit()

        self._call("role-delete", lambda core, cancel: core.agents("role-delete", role_id, cancel=cancel), removed)

    def load_usage(self, wanted):
        """What agent work used, summed by the core from the project's record
        (ADR-008) - files read; no agent is asked."""
        if self.core is None:
            return
        self.usage.skeleton.start()
        document = {"by": ["model", "connection", "kind", "role", "cycle"]}
        if wanted.get("since"):
            document["since"] = wanted["since"]

        def done(payload):
            self.usage.skeleton.stop()
            if payload.get("ok"):
                self.usage.say("")
                self.usage.set_usage(payload)
            else:
                self.usage.say(payload.get("error") or "The usage could not be read.")

        self._call("usage", lambda core, cancel: core.agents("usage", document=document, cancel=cancel), done)

    def _turns_of(self, field, value):
        """The turns behind a row of Usage: Activity, searched by that model or connection."""
        column = {"connection": 1, "model": 5}[field]
        self.tabs.setCurrentWidget(self.activity)
        box = self.activity.search.box(column)
        if box is not None:
            box.setText(value)

    # -- skills -----------------------------------------------------------------
    def load_skills(self, then=None):
        """The skills library - folders the core reads; nothing is fetched."""
        if self.core is None:
            return
        self.skills_tab.skeleton.start()

        def done(payload):
            self.skills_tab.skeleton.stop()
            if payload.get("ok"):
                self.skills_tab.say("")
                self.skills_tab.set_skills(payload.get("skills") or [], payload.get("own"))
                self.chat.set_library(payload.get("skills") or [], payload.get("own"))
            else:
                self.skills_tab.say(payload.get("error") or "The skills could not be read.")
            if then is not None:
                then()

        self._call("skills", lambda core, cancel: core.agents("skills", cancel=cancel), done)

    def look_at_skill(self, request, done):
        """Fetch a skill and say what is there. The one request on this page
        that reaches the network - and only because somebody pressed Look."""
        self._call("skill-look", lambda core, cancel: core.agents(
            "skill-look", document=dict(request), timeout=240, cancel=cancel), done)

    def look_at_skill_again(self, name, done):
        self._call("skill-update", lambda core, cancel: core.agents(
            "skill-update", name, timeout=240, cancel=cancel), done)

    def add_skills(self, document, done=None):
        """Put what was looked at into the library; the list is read again."""
        def answered(payload):
            if payload.get("ok"):
                self.load_skills()
            if done is not None:
                done(payload)

        self._call("skill-add", lambda core, cancel: core.agents(
            "skill-add", document=dict(document)), answered)

    def remove_skill(self, name, done=None):
        def answered(payload):
            if payload.get("ok"):
                self.load_skills()
            if done is not None:
                done(payload)

        self._call("skill-remove", lambda core, cancel: core.agents("skill-remove", name),
                   answered)
