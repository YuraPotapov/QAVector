"""What an issue shows of the agents' work on it, and the panel that hands it over.

The widgets of ``delegation.py``, the bridge between an opened issue and the
Agents integration. They show what they are given and say what was asked of
them; they ask the core nothing - the bridge does.

- **Agent work**: the conversations about the issue, a row each, opening in
  place into the conversation as the chat shows it, read-only - it goes on in
  the chat.
- **Summary**: what the work came to - conversations, turns, time, tokens, the
  cost where the agent's program reports it, how full each one's context is,
  and the files changed.
- **Changes**: each conversation's changes, folded until opened.
- **Delegate**: carry on a conversation, or begin one with the issue as its
  task.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox,
                               QHeaderView, QLabel, QPlainTextEdit, QPushButton, QScrollArea,
                               QFrame, QTableWidgetItem, QVBoxLayout, QWidget)

from .. import icons, theme, widgets
from ..diffview import DiffView, counts_text
from .agentwords import (ago_text, context_text, cost_text, count_text, folder_text,
                         tokens_text, tokens_total)

#: How the access a conversation works with reads on the panel. (label, mode)
MODES = (("Read", "read"), ("Edit", "edit"), ("Full", "full"))


def state_text(session):
    """Where a conversation is, as somebody deciding what to do next needs it."""
    if session.get("running"):
        return "Planning" if session.get("plan") else "Working"
    phase = session.get("phase")
    if phase == "questions":
        return "Questions for you"
    if phase == "plan":
        return "Plan ready"
    return {"completed": "Done", "failed": "Failed", "stopped": "Stopped",
            "interrupted": "Interrupted"}.get(session.get("state"), "Not begun")


def title_text(session, key=""):
    """A conversation's title on its issue's page, where the issue's key it
    begins with says nothing new."""
    title = session.get("title") or "Untitled"
    prefix = "%s · " % key if key else ""
    return title[len(prefix):] if prefix and title.startswith(prefix) else title


def took_seconds(session):
    total = 0.0
    for turn in session.get("turns") or []:
        if isinstance(turn.get("started"), (int, float)) and isinstance(
                turn.get("ended"), (int, float)):
            total += max(0.0, turn["ended"] - turn["started"])
    return total


def duration_text(seconds):
    seconds = int(round(seconds or 0))
    if seconds < 60:
        return "%d s" % seconds
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return "%d min %d s" % (minutes, seconds) if seconds else "%d min" % minutes
    hours, minutes = divmod(minutes, 60)
    return "%d h %d min" % (hours, minutes)


def _cell(text):
    item = QTableWidgetItem(str(text or ""))
    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
    return item


def _table(headers, expandable=False):
    table = widgets.Table(0, len(headers), reclick_clears=False, expandable=expandable)
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setShowGrid(False)
    table.setWordWrap(False)
    header = table.horizontalHeader()
    header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    header.setSectionResizeMode(0, QHeaderView.Stretch)
    return table


class _Shown(QWidget):
    """A tab that asks for what it shows the first time it is in front."""

    wanted = Signal()

    def showEvent(self, event):                 # noqa: N802 - Qt's own spelling
        super().showEvent(event)
        self.wanted.emit()


# -- Agent work -------------------------------------------------------------------
class ConversationDetail(QWidget):
    """One conversation opened in place: as the chat shows it, and the way there."""

    def __init__(self, session_id):
        super().__init__()
        from .agentchat import ChatFeed
        self.session_id = session_id
        column = QVBoxLayout(self)
        column.setContentsMargins(8, 4, 8, 8)
        column.setSpacing(6)
        self.open = QPushButton("Open in chat")
        self.open.setToolTip("Carry it on in Integrations -> Agents")
        column.addWidget(widgets.row(None, self.open))
        self.feed = ChatFeed()
        self.feed.set_empty_text("Reading the conversation...")
        column.addWidget(self.feed, 1)

    def fills_view(self):
        return True                             # a conversation needs room to be read

    def show_events(self, events):
        self.feed.set_empty_text("Nothing has been said in this conversation yet.")
        self.feed.load(events)


class AgentWorkTab(_Shown):
    """The conversations about the issue, newest first, each opening in place."""

    HEADERS = ("Conversation", "Agent", "State", "Turns", "Last")

    #: (session id) - the conversation is wanted in the chat.
    open_requested = Signal(str)
    #: (session id) - a row opened: its conversation is to be read.
    feed_wanted = Signal(str)

    def __init__(self):
        super().__init__()
        self.sessions = []
        column = QVBoxLayout(self)
        column.setContentsMargins(18, 12, 18, 12)
        column.setSpacing(8)
        column.addWidget(widgets.lede(
            "Conversations with an agent about this issue. A row opens into the "
            "conversation; it goes on in the agents' chat."))
        panel = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        self.table = _table(self.HEADERS, expandable=True)
        for index, width in ((1, 100), (2, 108), (3, 58), (4, 84)):
            self.table.setColumnWidth(index, width)
        self.table.itemClicked.connect(self._clicked)
        self.table.itemDoubleClicked.connect(self._double)
        self.table.toggle_requested.connect(self._toggle)
        self.search = widgets.SearchRow(self.table)
        self.search.changed.connect(self._say_empty)
        self.note = widgets.empty_note(self.table, "")
        panel.layout().addWidget(self.table)
        column.addWidget(panel, 1)

    def set_sessions(self, sessions, key=""):
        """The conversations, as the core lists them."""
        opened = {self.table.item(row, 0).data(Qt.UserRole)
                  for row in self.table.data_rows() if self.table.detail_of(row) is not None}
        self.sessions = list(sessions or [])
        self.table.setRowCount(0)
        for one in self.sessions:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = (title_text(one, key),
                      one.get("connection_name") or one.get("connection"),
                      state_text(one), str(len(one.get("turns") or [])),
                      ago_text(one.get("updated")))
            for column, value in enumerate(values):
                item = _cell(value)
                if column == 0:
                    item.setData(Qt.UserRole, one.get("id"))
                    item.setIcon(icons.icon("disclosure_closed", theme.NEUTRAL[600]))
                    item.setToolTip(folder_text(one.get("folder")))
                if column == 2 and state_text(one) in ("Questions for you", "Plan ready"):
                    item.setForeground(QColor(theme.WARN_INK))
                self.table.setItem(row, column, item)
        self.note.say("No agent has worked on %s yet. Delegate... hands it to one."
                      % (key or "this issue") if not self.sessions else "")
        self.search.apply()
        for row in self.table.data_rows():
            if self.table.item(row, 0).data(Qt.UserRole) in opened:
                self._toggle(row, True)

    def detail(self, session_id):
        for row in self.table.data_rows():
            if self.table.item(row, 0).data(Qt.UserRole) == session_id:
                return self.table.detail_of(row)
        return None

    def _clicked(self, item):
        row = self.table.owner_of(item.row())
        if item.column() == 0:
            self._toggle(row, self.table.detail_of(row) is None)

    def _double(self, item):
        row = self.table.owner_of(item.row())
        self.open_requested.emit(self.table.item(row, 0).data(Qt.UserRole))

    def _toggle(self, row, opened):
        first = self.table.item(row, 0)
        session_id = first.data(Qt.UserRole)
        first.setIcon(icons.icon("disclosure_open" if opened else "disclosure_closed",
                                 theme.NEUTRAL[600]))
        if not opened:
            self.table.close_row(row)
            return
        detail = ConversationDetail(session_id)
        detail.open.clicked.connect(lambda: self.open_requested.emit(session_id))
        self.table.open_row(row, detail)
        self.feed_wanted.emit(session_id)

    def _say_empty(self):
        self.note.say_filtered("No conversation matches the search."
                               if self.search.searching() else "")


# -- Summary -----------------------------------------------------------------------
class SummaryTab(_Shown):
    """What the agents' work on the issue came to."""

    HEADERS = ("Conversation", "Turns", "Tokens", "Context", "Cost", "State")

    def __init__(self):
        super().__init__()
        self.sessions, self.changes = [], {}
        column = QVBoxLayout(self)
        column.setContentsMargins(18, 12, 18, 12)
        column.setSpacing(8)
        self.figures = QLabel("")
        self.figures.setProperty("role", "chattitle")
        self.figures.setWordWrap(True)
        column.addWidget(self.figures)
        self.detail = QLabel("")
        self.detail.setProperty("role", "muted")
        self.detail.setWordWrap(True)
        column.addWidget(self.detail)
        panel = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        self.table = _table(self.HEADERS)
        for index, width in ((1, 58), (2, 62), (3, 100), (4, 56), (5, 92)):
            self.table.setColumnWidth(index, width)
        self.search = widgets.SearchRow(self.table)
        self.note = widgets.empty_note(self.table, "")
        panel.layout().addWidget(self.table)
        column.addWidget(panel, 1)

    def set_sessions(self, sessions, changes=None, key=""):
        """``changes`` is each conversation's changes as the core said them,
        by id - read when this tab is first in front."""
        self.sessions = list(sessions or [])
        self.changes = dict(changes or {})
        turns = sum(len(one.get("turns") or []) for one in self.sessions)
        tokens = {}
        for one in self.sessions:
            for name, value in (one.get("tokens") or {}).items():
                if isinstance(value, int):
                    tokens[name] = tokens.get(name, 0) + value
        priced = [one for one in self.sessions if any(
            isinstance(turn.get("cost_usd"), (int, float)) for turn in one.get("turns") or [])]
        cost = sum(one.get("cost_usd") or 0 for one in priced)
        files = {}
        for found in self.changes.values():
            for entry in found.get("files") or []:
                files[entry.get("path")] = entry
        if not self.sessions:
            self.figures.setText("No agent has worked on %s yet." % (key or "this issue"))
            self.detail.setText("")
        else:
            parts = ["%d conversation%s" % (len(self.sessions), "" if len(self.sessions) == 1
                                             else "s"),
                     "%d turn%s" % (turns, "" if turns == 1 else "s"),
                     duration_text(sum(took_seconds(one) for one in self.sessions))]
            if priced:
                parts.append(cost_text(cost) or "$0.00")
            self.figures.setText(" · ".join(parts))
            lines = []
            if tokens:
                lines.append("Tokens: %s in all - %s." % (count_text(tokens_total(tokens)),
                                                          tokens_text(tokens)))
            if len(priced) < len(self.sessions):
                lines.append("The cost is only what the agents' programs reported; %d "
                             "conversation%s said none." % (
                                 len(self.sessions) - len(priced),
                                 "" if len(self.sessions) - len(priced) == 1 else "s"))
            if self.changes:
                lines.append("Files changed: %d (%s) across the conversations' folders." % (
                    len(files), counts_text(sum(one.get("added") or 0 for one in files.values()),
                                            sum(one.get("removed") or 0
                                                for one in files.values()))))
            self.detail.setText("\n".join(lines))
        self.table.setRowCount(0)
        for one in self.sessions:
            row = self.table.rowCount()
            self.table.insertRow(row)
            mine = one.get("tokens") or {}
            values = (title_text(one, key),
                      str(len(one.get("turns") or [])),
                      count_text(tokens_total(mine)) if mine else "",
                      context_text(one.get("context")),
                      cost_text(one.get("cost_usd")) if one in priced else "",
                      state_text(one))
            for column, value in enumerate(values):
                item = _cell(value)
                if column == 0:
                    item.setToolTip(" · ".join(part for part in (
                        one.get("title"), one.get("connection_name") or one.get("connection"),
                        folder_text(one.get("folder"))) if part))
                if column == 2 and mine:
                    item.setToolTip(tokens_text(mine))
                self.table.setItem(row, column, item)
        self.note.say("" if self.sessions else "Nothing to sum up yet.")
        self.search.apply()


# -- Changes -----------------------------------------------------------------------
class ChangesTab(_Shown):
    """Each conversation's changes, folded until opened."""

    #: (session id) - a conversation's changes are to be read.
    changes_wanted = Signal(str)

    def __init__(self):
        super().__init__()
        self.sections = {}
        column = QVBoxLayout(self)
        column.setContentsMargins(18, 12, 18, 12)
        column.setSpacing(8)
        column.addWidget(widgets.lede(
            "What changed in each conversation's folder while its agent worked - "
            "whoever changed it there at the same time."))
        self.empty = QLabel("")
        self.empty.setProperty("role", "hint")
        column.addWidget(self.empty)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        self.list = QVBoxLayout(body)
        self.list.setContentsMargins(0, 0, 0, 0)
        self.list.setSpacing(6)
        self.list.addStretch(1)
        self.scroll.setWidget(body)
        column.addWidget(self.scroll, 1)

    def set_sessions(self, sessions, changes=None, key=""):
        changes = changes or {}
        opened = {sid for sid, section in self.sections.items() if section.is_expanded()}
        for section in self.sections.values():
            self.list.removeWidget(section)
            section.setParent(None)
            section.deleteLater()
        self.sections = {}
        for one in sessions or []:
            sid = one.get("id")
            found = changes.get(sid)
            title = title_text(one, key)
            if found is not None:
                files = found.get("files") or []
                title += " · %s" % ("no files changed" if not files else "%d file%s %s" % (
                    len(files), "" if len(files) == 1 else "s",
                    counts_text(found.get("added"), found.get("removed"))))
            section = widgets.Disclosure(title, expanded=False)
            view = DiffView(scrolling=False)
            view.summary.setText("Reading..." if found is None else "")
            if found is not None:
                view.set_changes(found)
            section.body().addWidget(view)
            section.view = view
            section.toggled.connect(lambda on, sid=sid: on and self.changes_wanted.emit(sid))
            self.list.insertWidget(len(self.sections), section)
            self.sections[sid] = section
            if sid in opened:
                section.set_expanded(True)
        self.empty.setText("" if sessions else
                           "No agent has worked on %s yet." % (key or "this issue"))


# -- the panel ---------------------------------------------------------------------
class DelegatePanel(widgets.FloatingPanel):
    """Hand the issue to an agent: carry on a conversation about it, or begin one."""

    #: (session id) - carry this one on.
    continue_requested = Signal(str)
    #: (chosen, message) - begin one: ``connection``, ``folder``, ``mode``, ``plan``.
    delegate_requested = Signal(dict, str)

    WIDTH = 600
    SHOWN = 5                                   # conversations offered to carry on

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("role", "clarifypopup")
        self.setFixedWidth(self.WIDTH)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.heading = widgets.kicker("Delegate")
        head = widgets.row(self.heading, None)
        head.setProperty("role", "clarifyhead")
        head.layout().setContentsMargins(12, 7, 12, 7)
        outer.addWidget(head)
        body = QWidget()
        column = QVBoxLayout(body)
        column.setContentsMargins(12, 10, 12, 12)
        column.setSpacing(8)
        self.earlier_label = QLabel("Carry on a conversation about it")
        self.earlier_label.setProperty("role", "composerlabel")
        column.addWidget(self.earlier_label)
        self.earlier = QWidget()
        self.earlier_rows = QVBoxLayout(self.earlier)
        self.earlier_rows.setContentsMargins(0, 0, 0, 0)
        self.earlier_rows.setSpacing(2)
        column.addWidget(self.earlier)
        self.new_label = QLabel("Or begin a new one")
        self.new_label.setProperty("role", "composerlabel")
        column.addWidget(self.new_label)
        self.connection = widgets.capped(QComboBox(), 200)
        self.connection.setAccessibleName("Agent")
        self.mode = widgets.Segmented([label for label, _mode in MODES])
        self.mode.setToolTip("What the agent may do once it works: read, change files, "
                             "or also run commands.")
        self.plan = QCheckBox("Plan first")
        self.plan.setToolTip("The agent looks first, changing nothing, and answers with "
                             "questions or a plan; it works once you approve the plan.")
        column.addWidget(widgets.row(QLabel("Agent"), self.connection, 12, QLabel("Access"),
                                     self.mode, 12, self.plan, None))
        self.folder = widgets.ElidedLabel(mode=Qt.ElideMiddle)
        self.folder.setProperty("mono", "true")
        self.change = QPushButton("Change…")
        self.change.setProperty("variant", "ghost")
        self.change.clicked.connect(self._pick_folder)
        folder_row = widgets.row(QLabel("Folder"), self.folder, self.change)
        folder_row.layout().setStretchFactor(self.folder, 1)
        column.addWidget(folder_row)
        self.text = QPlainTextEdit()
        self.text.setAccessibleName("Task")
        self.text.setMinimumHeight(180)
        column.addWidget(self.text, 1)
        self.say_label = QLabel("")
        self.say_label.setProperty("role", "composernote")
        self.say_label.setWordWrap(True)
        self.say_label.setVisible(False)
        column.addWidget(self.say_label)
        self.cancel = QPushButton("Cancel")
        self.cancel.clicked.connect(self.hide)
        self.go = QPushButton("Delegate")
        self.go.setProperty("variant", "primary")
        self.go.clicked.connect(self._go)
        column.addWidget(widgets.row(None, self.cancel, self.go))
        outer.addWidget(body)
        self._folder = ""

    def set_offer(self, key, sessions, connections, chosen, text):
        """What is offered for ``key``: its conversations, the agents, the
        choices to begin with - ``connection``, ``folder``, ``mode``, ``plan`` -
        and the task as it would be said."""
        self.heading.setText("Delegate %s" % key)
        while self.earlier_rows.count():
            item = self.earlier_rows.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        shown = [one for one in sessions or [] if not one.get("record")][:self.SHOWN]
        for one in shown:
            said = " · ".join(part for part in (
                title_text(one, key), state_text(one), ago_text(one.get("updated")),
                one.get("connection_name") or "", folder_text(one.get("folder"))) if part)
            label = widgets.ElidedLabel(mode=Qt.ElideRight)
            label.setText(said)
            label.setToolTip(said)
            go = QPushButton("Carry on")
            go.setProperty("variant", "ghost")
            go.clicked.connect(lambda _c=False, sid=one.get("id"): self._carry_on(sid))
            row = widgets.row(label, go)
            row.layout().setStretchFactor(label, 1)
            self.earlier_rows.addWidget(row)
        self.earlier_label.setVisible(bool(shown))
        self.earlier.setVisible(bool(shown))
        self.new_label.setText("Or begin a new one" if shown else "Begin a conversation")
        self.connection.clear()
        for one in connections or []:
            self.connection.addItem(one.get("name") or one.get("id"), one.get("id"))
        index = self.connection.findData(chosen.get("connection"))
        self.connection.setCurrentIndex(index if index >= 0 else (0 if self.connection.count()
                                                                  else -1))
        labels = {mode: label for label, mode in MODES}
        self.mode.set_current(labels.get(chosen.get("mode"), "Edit"), notify=False)
        self.plan.setChecked(bool(chosen.get("plan", True)))
        self.set_folder(chosen.get("folder") or "")
        self.text.setPlainText(text)
        self.say("")

    def set_folder(self, folder):
        self._folder = folder or ""
        self.folder.setText(folder_text(self._folder) or "No folder chosen yet")
        self.folder.setToolTip(self._folder)

    def chosen(self):
        return {"connection": self.connection.currentData() or "", "folder": self._folder,
                "mode": dict(MODES)[self.mode.current()], "plan": self.plan.isChecked()}

    def say(self, text):
        self.say_label.setText(text or "")
        self.say_label.setVisible(bool(text))

    def open_below(self, anchor):
        self.adjustSize()
        corner = anchor.mapToGlobal(anchor.rect().bottomLeft())
        screen = QApplication.screenAt(corner) or QApplication.primaryScreen()
        area = screen.availableGeometry() if screen is not None else None
        x, y = corner.x(), corner.y() + 4
        if area is not None:
            x = max(area.left(), min(x, area.right() - self.width()))
            y = max(area.top(), min(y, area.bottom() - self.height()))
        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()
        self.text.setFocus()

    def keyPressEvent(self, event):              # noqa: N802 - Qt's own spelling
        if event.key() == Qt.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)

    def _pick_folder(self):
        chosen = widgets.pick_path(self, "The folder the agent works in", self._folder or "~",
                                   directory=True)
        if chosen:
            self.set_folder(chosen)

    def _carry_on(self, session_id):
        self.hide()
        self.continue_requested.emit(session_id)

    def _go(self):
        chosen = self.chosen()
        if not chosen["connection"]:
            self.say("There is no agent connection yet: add one under Integrations -> Agents.")
            return
        if not chosen["folder"]:
            # Never a guess: an agent let loose in the wrong folder is what the
            # choice is there to prevent.
            self.say("Choose the folder the agent works in.")
            return
        if not self.text.toPlainText().strip():
            self.say("Say what the task is.")
            return
        self.delegate_requested.emit(chosen, self.text.toPlainText().strip())
