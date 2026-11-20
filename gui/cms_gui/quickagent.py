"""A global new-chat draft; the existing ChatTab owns every resulting turn."""

import os
import time

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout)
from shiboken6 import isValid

from . import restrictions, widgets
from .pages.agentchat import Composer, SESSIONS_SHOWN, _is_chat
from .pages.agentwords import ago_text, folder_text


RECENT_LIMIT = 5
CONNECTIONS_PATH = "Integrations > Agents > Connections"


class _ComposerArea(QScrollArea):
    """Let a long draft or expanded settings scroll inside the fixed panel."""

    def __init__(self, composer):
        super().__init__()
        self.setFrameShape(QFrame.NoFrame)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setMinimumHeight(0)
        self.setMaximumHeight(280)
        self.setWidget(composer)
        composer.installEventFilter(self)

    def sizeHint(self):
        return QSize(0, min(280, self.widget().sizeHint().height()))

    def eventFilter(self, watched, event):
        if watched is self.widget() and event.type() == QEvent.LayoutRequest:
            self.updateGeometry()
        return super().eventFilter(watched, event)


class _ConversationRow(QFrame):
    """A conversation title, its context, and quiet metadata on the right."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setProperty("role", "quickrecent")
        self.setAttribute(Qt.WA_Hover)
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 10, 16, 10)
        row.setSpacing(16)
        words = QVBoxLayout()
        words.setSpacing(4)
        self.title = widgets.ElidedLabel(mode=Qt.ElideRight, parent=self)
        self.title.setProperty("role", "quickrecenttitle")
        self.detail = widgets.ElidedLabel(mode=Qt.ElideMiddle, parent=self)
        self.detail.setProperty("role", "quickrecentdetail")
        self.title.setTextFormat(Qt.PlainText)
        self.detail.setTextFormat(Qt.PlainText)
        words.addWidget(self.title)
        words.addWidget(self.detail)
        row.addLayout(words, 1)
        meta = QVBoxLayout()
        meta.setSpacing(4)
        self.when = QLabel(self)
        self.state = QLabel(self)
        for label in (self.when, self.state):
            label.setProperty("role", "quickrecentdetail")
            label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            meta.addWidget(label)
        row.addLayout(meta)

    def set_current(self, current):
        value = "true" if current else "false"
        if self.property("current") != value:
            self.setProperty("current", value)
            for widget in (self, self.title, self.detail, self.when, self.state):
                widget.style().unpolish(widget)
                widget.style().polish(widget)


class QuickAgentPopup(widgets.FloatingPanel):
    """An in-memory draft over any page, using the project's existing host."""

    chat_wanted = Signal(str)
    WIDTH = 960
    HEIGHT = 560

    def __init__(self, agents, shortcut, parent=None):
        super().__init__(parent)
        self.setProperty("role", "clarifypopup")
        self.setWindowTitle("Quick Agent")
        self.agents = agents
        self.chat = agents.chat
        self._shortcut = shortcut
        self._previous_focus = None
        self._pending = False
        self._ready = False
        self._kinds_ready = False
        self._kinds_error = ""
        self._wanted = {}
        self._recent = []
        self._recent_error = ""
        self._recent_loading = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        head = QFrame()
        head.setProperty("role", "quickagenthead")
        band = QHBoxLayout(head)
        band.setContentsMargins(16, 10, 12, 10)
        band.setSpacing(12)
        title = QLabel("Quick Agent")
        title.setProperty("role", "chattitle")
        mode = QLabel("New chat")
        mode.setProperty("role", "muted")
        self.keys = QLabel()
        self.keys.setProperty("role", "keys")
        close = QPushButton("×")
        close.setProperty("variant", "ghost")
        close.setAccessibleName("Close Quick Agent")
        close.setToolTip("Close (Esc)")
        close.setFixedWidth(28)
        close.clicked.connect(self.dismiss)
        band.addWidget(title)
        band.addWidget(mode)
        band.addStretch(1)
        band.addWidget(self.keys)
        band.addWidget(close)
        outer.addWidget(head)

        self.composer = Composer()
        self.composer.text.setAccessibleName("Quick Agent message")
        self.composer.text.setPlaceholderText("Write a task — Enter sends, Shift+Enter starts a line")
        # The MVP keeps skill selection in the full chat; inherited choices are
        # still visible in the summary and passed through the shared lifecycle.
        self.composer.skills_button.hide()
        self.composer.submitted.connect(self.submit)
        self.composer.restrictions_requested.connect(self.edit_restrictions)
        self.composer.model.requested.connect(self._load_models)
        for picker in (self.composer.connection, self.composer.model, self.composer.effort):
            picker.activated.connect(self._focus_editor)
        self.composer.browse.clicked.connect(self._focus_editor)
        self.composer.content_changed.connect(self._sync_send)
        self.composer_area = _ComposerArea(self.composer)
        outer.addWidget(self.composer_area)

        view_all = QPushButton("View all")
        view_all.setProperty("variant", "ghost")
        view_all.clicked.connect(lambda: self._open_chat(""))
        self.view_all = view_all
        section = QHBoxLayout()
        section.setContentsMargins(16, 8, 12, 6)
        section.addWidget(widgets.kicker("Recent conversations"))
        section.addStretch(1)
        section.addWidget(view_all)
        outer.addLayout(section)
        self.recent = QListWidget()
        self.recent.setAccessibleName("Recent agent conversations")
        self.recent.setProperty("role", "quickrecentlist")
        self.recent.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.recent.setMinimumHeight(64)
        self.recent.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        self.recent.itemClicked.connect(self._open_recent)
        self.recent.itemActivated.connect(self._open_recent)
        self.recent.currentRowChanged.connect(self._recent_selection_changed)
        outer.addWidget(self.recent, 1)
        self.recent_skeleton = widgets.Skeleton(self.recent, "rows")
        hint = QLabel("Enter — Send    Shift+Enter — New line    Tab — Navigate    Esc — Close")
        hint.setProperty("role", "keys")
        hint.setWordWrap(True)
        hint.setContentsMargins(16, 10, 16, 12)
        outer.addWidget(hint)
        agents.connections_changed.connect(self._connections_changed)
        agents.connections_loaded.connect(self._connections_read)
        self.chat.conversation_changed.connect(self._conversation_changed)
        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._fill_recent)

    def has_draft(self):
        return bool(self.composer.draft().strip() or self.composer.attachments())

    def reset(self):
        self.hide()
        self.composer.set_draft("")
        self.composer.set_attachments([])
        self.composer.set_session(None)
        self._recent = []
        self._recent_error = ""
        self._recent_loading = False
        self._ready = self._kinds_ready = False

    def open_over(self, window=None):
        if self.isVisible():
            self._focus_editor()
            return
        self._previous_focus = QApplication.focusWidget()
        self.keys.setText(self._shortcut() or "Unassigned")
        if not self._pending:
            self._wanted = (dict(self.composer.settings(),
                                restrictions=self.composer.restrictions(), skills=self.composer.skills())
                            if self.has_draft() and self._ready else self.chat._defaults())
            self._ready = self._kinds_ready = False
            self._kinds_error = ""
            self.composer.set_connections(self.agents.connections)
            self.composer.set_folders(self.chat._memory().get("folders") or [])
            self.composer.set_session(None, self._wanted)
            self.composer.say("Reading Connections…")
        self.composer.set_options_open(False)
        self._sync_send()
        window = window or self.parentWidget()
        screen = window.screen() if window is not None else QApplication.primaryScreen()
        available = screen.availableGeometry()
        self.setFixedSize(min(self.WIDTH, available.width() - 24),
                          min(self.HEIGHT, available.height() - 24))
        frame = window.frameGeometry() if window is not None else available
        x = frame.x() + (frame.width() - self.width()) // 2
        y = frame.y() + max(0, (frame.height() - self.height()) // 3)
        self.move(max(available.left(), min(x, available.right() - self.width() + 1)),
                  max(available.top(), min(y, available.bottom() - self.height() + 1)))
        self.show()
        self.raise_()
        self.activateWindow()
        self._focus_editor()
        if not self._pending:
            if self.agents.core is None:
                self.composer.say("The project core is unavailable. Check application settings.", bad=True)
            else:
                self.agents.list_connections()
                self.agents.ask("quick-agent-kinds", lambda core, cancel: core.agents(
                    "kinds", cancel=cancel), self._kinds_read)
                self._load_recent()
        self._tick.start()

    def _focus_editor(self, *_args):
        if self.isVisible():
            self.composer.text.setFocus(Qt.OtherFocusReason)

    def dismiss(self):
        self.hide()
        previous = self._previous_focus
        if previous is not None and isValid(previous) and previous.isVisible():
            previous.window().activateWindow()
            previous.setFocus(Qt.OtherFocusReason)

    def hideEvent(self, event):
        self._tick.stop()
        self.recent_skeleton.stop()
        super().hideEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape and not self.composer.text.composing:
            self.dismiss()
            event.accept()
            return
        super().keyPressEvent(event)

    def _connections_changed(self):
        if not self.isVisible() or self._pending:
            return
        chosen = self.composer.settings() if self._ready else self._wanted
        if not any(one.get("id") == chosen.get("connection") for one in self.agents.connections):
            # A removed connection's draft can fall back, but its model and
            # effort cannot become overrides for a different provider.
            chosen = dict(chosen, model="", effort="", others=None)
        options_open = self.composer.settings_button.isChecked()
        self.composer.set_connections(self.agents.connections)
        self.composer.set_session(None, dict(chosen,
            restrictions=self.composer.restrictions(), skills=self.composer.skills()))
        self.composer.set_options_open(options_open)
        self._ready = True
        self.composer.say("" if self.agents.connections else
                          "No Connections configured. Add one in " + CONNECTIONS_PATH + ".")
        if self._kinds_error:
            self.composer.say(self._kinds_error, bad=True)
        elif self.agents.connections and not self._kinds_ready:
            self.composer.say("Reading provider capabilities…")
        for index, connection in enumerate(self.agents.connections):
            check = connection.get("last_check") or {}
            self.composer.connection.setItemData(index,
                check.get("error") or check.get("account") or "Connection has not been checked.",
                Qt.ToolTipRole)
        self._show_availability()
        self._sync_send()

    def _connections_read(self, payload):
        if self.isVisible() and not payload.get("ok"):
            self._ready = False
            self.composer.say(payload.get("error") or
                              "Connections could not be read. Check " + CONNECTIONS_PATH + ".", bad=True)
            self._sync_send()

    def _kinds_read(self, payload):
        self._kinds_ready = bool(payload.get("ok"))
        if self._kinds_ready:
            self._kinds_error = ""
            self.composer.set_kinds(payload.get("kinds") or [])
            self._show_availability()
            if self._ready and self.agents.connections:
                self.composer.say("")
        else:
            self._kinds_error = payload.get("error") or "Provider capabilities could not be read."
            self.composer.say(self._kinds_error, bad=True)
        self._sync_send()

    def _show_availability(self):
        if not self._kinds_ready:
            return
        kinds = {one.get("kind"): one for one in self.composer._kinds}
        for index, connection in enumerate(self.agents.connections):
            kind = kinds.get(connection.get("kind")) or {}
            available = bool(kind and (kind.get("found") or connection.get("binary")))
            name = connection.get("name") or connection.get("id")
            self.composer.connection.setItemText(index, name if available else name + " (unavailable)")
            if not available:
                self.composer.connection.setItemData(index,
                    "Provider program not found. Check " + CONNECTIONS_PATH + ".", Qt.ToolTipRole)

    def _sync_send(self):
        self.composer.send_button.setEnabled(self.has_draft() and self._ready
            and self._kinds_ready and bool(self.agents.connections) and not self._pending)

    def _load_models(self, done):
        connection = self.composer.connection.currentData()
        if not connection or self.agents.core is None:
            done({"ok": False, "error": "Choose a connection first."})
            return
        self.agents.ask("quick-agent-models", lambda core, cancel: core.agents(
            "models", connection, timeout=30, cancel=cancel), done)

    def _load_recent(self):
        if self.agents.core is None:
            return
        self._recent_loading = True
        self.recent.setEnabled(False)
        self.recent_skeleton.start()
        self.agents.ask("quick-agent-sessions", lambda core, cancel: core.agents(
            "sessions", document={"limit": SESSIONS_SHOWN, "sidebar": True}, cancel=cancel), self._recent_read)

    def _recent_read(self, payload):
        self._recent_loading = False
        self.recent_skeleton.stop()
        self.recent.setEnabled(True)
        if payload.get("ok"):
            self._recent_error = ""
            self._recent = sorted((one for one in payload.get("sessions") or [] if _is_chat(one)),
                                  key=lambda one: one.get("updated") or 0, reverse=True)[:RECENT_LIMIT]
            self._fill_recent()
        else:
            self._recent_error = payload.get("error") or "Recent conversations could not be read."
            self._fill_recent()

    def _conversation_changed(self, _session_id):
        if self.isVisible():
            self._load_recent()

    def _fill_recent(self):
        if self._recent_loading:
            return
        selected = self.recent.currentItem()
        selected_id = selected.data(Qt.UserRole) if selected is not None else None
        empty = self._recent_error or ("No conversations yet. Write a task to start one."
                                      if not self._recent else "")
        if empty:
            if self.recent.count() != 1 or self.recent.item(0).text() != empty:
                self.recent.clear()
                item = QListWidgetItem(empty)
                item.setFlags(Qt.NoItemFlags)
                item.setSizeHint(QSize(0, 46))
                self.recent.addItem(item)
            return
        ids = [one.get("id") for one in self._recent]
        existing = [self.recent.item(index).data(Qt.UserRole) for index in range(self.recent.count())]
        if existing != ids:
            self.recent.clear()
            for sid in ids:
                item = QListWidgetItem()
                item.setData(Qt.UserRole, sid)
                self.recent.addItem(item)
                row = _ConversationRow(self.recent)
                item.setSizeHint(QSize(0, max(64, row.sizeHint().height())))
                self.recent.setItemWidget(item, row)
                if selected_id == sid:
                    self.recent.setCurrentItem(item)
        for index, session in enumerate(self._recent):
            sid = session.get("id")
            state = "Answering" if sid in self.chat.turns else session.get("state") or "Ready"
            description = " · ".join((session.get("connection_name") or session.get("connection") or "",
                                      folder_text(session.get("folder")) or "No folder"))
            title = (session.get("title") or "Untitled").replace("\n", " ")
            when = ago_text(session.get("updated"), time.time())
            item = self.recent.item(index)
            item.setData(Qt.AccessibleTextRole, " · ".join((title, description, state, when)))
            item.setToolTip(title + "\n" + description)
            row = self.recent.itemWidget(item)
            row.title.setText(title)
            row.detail.setText(description)
            row.when.setText(when)
            row.state.setText(state.capitalize())
        self._recent_selection_changed()

    def _recent_selection_changed(self, *_args):
        for index in range(self.recent.count()):
            row = self.recent.itemWidget(self.recent.item(index))
            if row is not None:
                row.set_current(index == self.recent.currentRow())

    def _open_recent(self, item):
        if item.data(Qt.UserRole) and self.isVisible():
            self._open_chat(item.data(Qt.UserRole))

    def _open_chat(self, session_id):
        if not self._pending:
            self.hide()
            self.chat_wanted.emit(session_id)

    def edit_restrictions(self):
        folder = self.composer.settings()["folder"]
        if not folder:
            return
        dialog = restrictions.RestrictionsWindow(
            folder_text(folder), folder, "project", self.composer.restrictions(),
            core=self.agents.core, probe={"folder": folder, "cwd": folder}, parent=self)
        if dialog.exec() == QDialog.Accepted and dialog.saved is not None:
            self.composer.set_restrictions(dialog.saved)
        dialog.deleteLater()
        self._focus_editor()

    def submit(self, message):
        if self._pending or not self.has_draft():
            return
        if self._ready and not self.agents.connections:
            self.composer.say("No Connections configured. Add one in " + CONNECTIONS_PATH + ".", bad=True)
            return
        if not self._ready or not self._kinds_ready or self.agents.core is None:
            self.composer.say("Wait for Connections and provider capabilities to load.", bad=True)
            return
        chosen = self.composer.settings()
        connection = next((one for one in self.agents.connections
                           if one.get("id") == chosen["connection"]), None)
        kind = next((one for one in self.composer._kinds
                     if connection and one.get("kind") == connection.get("kind")), {})
        error = ""
        if connection is None:
            error = "No Connections configured. Add one in " + CONNECTIONS_PATH + "."
        elif not kind or not (kind.get("found") or connection.get("binary")):
            error = "This provider is unavailable. Check its program in " + CONNECTIONS_PATH + "."
        elif not chosen["folder"] or not os.path.isdir(chosen["folder"]):
            error = "Choose an existing folder the agent can work in."
        elif not os.access(chosen["folder"], os.R_OK | os.X_OK):
            error = "The selected folder cannot be accessed."
        elif chosen["mode"] not in kind.get("modes", []):
            error = "This provider does not support the selected access mode."
        elif chosen["effort"] and chosen["effort"] not in kind.get("efforts", []):
            error = "This provider does not support the selected effort."
        if error:
            self.composer.say(error, bad=True)
            return
        chosen.update(restrictions=self.composer.restrictions(), skills=self.composer.skills(),
                      attachments=self.composer.attachments())
        self._pending = True
        self.composer.setEnabled(False)
        self._sync_send()

        def done(payload):
            self._pending = False
            self.composer.setEnabled(True)
            self._sync_send()
            if not payload.get("ok"):
                self.composer.say(payload.get("error") or "The conversation could not be started.", bad=True)
                self._focus_editor()
                return
            self.composer.set_draft("")
            self.composer.set_attachments([])
            self.hide()
            # delegate has already opened the session and connected its stream.
            self.chat_wanted.emit("")

        if not self.chat.delegate(chosen, {"label": "Chat"}, message, done):
            done({"ok": False, "error": "Another conversation is being created. Try again."})
