"""Integrations -> Teams: post to a channel, see what was posted, set channels up (QCR ADR-015 D6).

Everything goes through the core (``--integrations``): the page never holds a
webhook. A connection is one channel or chat; its webhook is shown as its host
only, and a new one is typed into a field that never shows it back.

* **New post** - the channel, a title, the text (Markdown as a release note
  is: lines, ``- `` bullets, ``**bold**``), people to mention by a name the
  channel knows or their sign-in address, and the card as it will look. Send
  asks first: a post cannot be taken back. One post a minute a channel.
* **History** - what QAVector posted through a channel, newest first, and
  Send again: a new post with the same words, asked first too.
* **Connections** - the channels: read-only until Edit; the webhook, the
  default title and footer, the names to mention people by, and what agents and
  cycles may do through the channel by default (D5). A test post is a real one.
"""

import html
import threading

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QCompleter, QDialog, QFormLayout, QFrame, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QPlainTextEdit, QPushButton, QScrollArea, QSplitter, QTabWidget,
                               QTextBrowser, QVBoxLayout, QWidget)

from .. import background, core as core_mod, search, widgets
from .jiraconnections import _cell, _table

PROVIDER = "teams"
TARGETS = (("Channel", "channel"), ("Group chat", "chat"), ("Person", "person"))
#: What agents and cycles may do through a channel by default (ADR-015 D5) -
#: the same words as a conversation's delegation settings.
AGENTS = (("Off", "off"), ("Ask", "approve"), ("Auto", "post"))
CYCLES = (("Off", "off"), ("Auto", "post"))
OUTCOMES = {"accepted": "Accepted", "failed": "Failed", "unknown": "Unknown"}
WRITES = ("save", "delete", "post", "again", "test")
SEND_ASK = "A post cannot be taken back. It goes to %s now, as the card shows it."


def _words(pairs, value):
    return next((label for label, one in pairs if one == value), value or "")


def _when(at):
    import time
    return time.strftime("%d %b %H:%M", time.localtime(at)) if at else ""


def card_html(title, text, footer="", mentions=()):
    """The post as the card draws it: the title, then a block a line, a bullet drawn
    as one - the same layout the core sends (``integrations.teams.webhook.card``)."""
    lines = []
    if title.strip():
        lines.append("<p><b>%s</b></p>" % html.escape(title.strip()))
    body = text.rstrip() + ("\n\n" + footer if footer else "")
    tags = ["@" + one for one in mentions]
    if tags:
        body += "\n\n" + ", ".join(tags)
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        shown = html.escape(stripped[2:] if stripped.startswith(("- ", "* ")) else stripped)
        shown = shown.replace("**", "")
        lines.append("<p>%s%s</p>" % ("&#8226; " if stripped.startswith(("- ", "* ")) else "", shown))
    return "".join(lines) or "<p><i>The card is empty: write the text.</i></p>"


class NewPostTab(QWidget):
    send_wanted = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.connection = {}
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 14, 0, 0)
        outer.setSpacing(14)
        form = QWidget()
        column = QVBoxLayout(form)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        self.title = QLineEdit()
        self.title.setObjectName("teamsPostTitle")
        self.text = QPlainTextEdit()
        self.text.setObjectName("teamsPostText")
        self.text.setPlaceholderText("What to post - lines, \"- \" bullets, **bold**.")
        self.mentions = QLineEdit()
        self.mentions.setObjectName("teamsPostMentions")
        self.mentions.setPlaceholderText("Names the channel knows, or sign-in addresses, comma-separated")
        column.addWidget(widgets.field("Title", self.title))
        column.addWidget(widgets.field("Text", self.text), 1)
        column.addWidget(widgets.field("Mention", self.mentions))
        self.message = QLabel("")
        self.message.setProperty("role", "hint")
        self.message.setWordWrap(True)
        self.send = QPushButton("Send")
        self.send.setProperty("variant", "primary")
        self.send.clicked.connect(self._send)
        column.addWidget(widgets.row(self.message, None, self.send))
        outer.addWidget(form, 3)
        right = QWidget()
        stack = QVBoxLayout(right)
        stack.setContentsMargins(0, 0, 0, 0)
        stack.setSpacing(4)
        caption = QLabel("THE CARD")
        caption.setProperty("role", "statlabel")
        stack.addWidget(caption)
        self.preview = QTextBrowser()
        self.preview.setObjectName("teamsCardPreview")
        self.preview.setProperty("role", "answer")
        stack.addWidget(self.preview, 1)
        outer.addWidget(right, 2)
        for signal in (self.title.textChanged, self.text.textChanged, self.mentions.textChanged):
            signal.connect(self._sync)
        widgets.see_through(self)
        self._sync()

    def set_connection(self, connection):
        self.connection = dict(connection or {})
        settings = self.connection.get("settings") or {}
        self.title.setPlaceholderText(settings.get("title") or "No title")
        completer = QCompleter(sorted(settings.get("aliases") or {}), self.mentions)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.mentions.setCompleter(completer)
        self._sync()

    def names(self):
        return [one.strip() for one in self.mentions.text().split(",") if one.strip()]

    def has_draft(self):
        return bool(self.text.toPlainText().strip())

    def clear(self):
        for field in (self.title, self.mentions):
            field.clear()
        self.text.clear()

    def say(self, text):
        self.message.setText(text or "")
        self.message.setProperty("hint", "false")

    def _sync(self):
        settings = self.connection.get("settings") or {}
        aliases = {name.lower() for name in settings.get("aliases") or {}}
        unknown = [one for one in self.names() if "@" not in one and one.lstrip("@").lower() not in aliases]
        self.preview.setHtml(card_html(self.title.text() or settings.get("title") or "", self.text.toPlainText(),
                                       settings.get("footer") or "", self.names()))
        ready = bool(self.connection.get("id")) and self.has_draft() and not unknown
        self.send.setEnabled(ready)
        hint = ("Not a name this channel knows, nor an address: %s." % ", ".join(unknown) if unknown
                else "" if self.connection.get("id") else "Add a channel in Connections first.")
        # The form's own hints come and go with it; what a send said stays until the next.
        if hint or self.message.property("hint") == "true":
            self.say(hint)
            self.message.setProperty("hint", "true" if hint else "false")

    def _send(self):
        name = self.connection.get("name") or "the channel"
        if not widgets.confirm(self, "Post to Teams", "Post to %s?" % name, SEND_ASK % name, agree="Send"):
            return
        self.send_wanted.emit({"title": self.title.text().strip(), "text": self.text.toPlainText().strip(),
                               "mentions": self.names()})


class HistoryTab(QWidget):
    HEADERS = ("When", "Title", "Outcome", "From", "HTTP")
    again_wanted = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.posts = []
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 14, 0, 0)
        column.setSpacing(8)
        split = QSplitter(Qt.Vertical)
        panel = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        self.table = _table(self.HEADERS)
        self.table.setObjectName("teamsHistory")
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for index, width in ((0, 120), (2, 96), (3, 160), (4, 60)):
            self.table.setColumnWidth(index, width)
        self.table.itemSelectionChanged.connect(self._picked)
        self.search = widgets.SearchRow(self.table)
        self.search.changed.connect(self._say_empty)
        self.note = widgets.empty_note(self.table, "")
        panel.layout().addWidget(self.table)
        split.addWidget(panel)
        detail = QWidget()
        stack = QVBoxLayout(detail)
        stack.setContentsMargins(0, 8, 0, 0)
        self.shown = QTextBrowser()
        self.shown.setProperty("role", "answer")
        stack.addWidget(self.shown, 1)
        self.again = QPushButton("Send again")
        self.again.setToolTip("A new post with these words. The one before stays as it is.")
        self.again.clicked.connect(self._again)
        stack.addWidget(widgets.row(None, self.again))
        split.addWidget(detail)
        column.addWidget(split, 1)
        self.set_posts([])

    def set_posts(self, posts):
        self.posts = list(posts or [])
        self.table.setRowCount(0)
        for post in self.posts:
            index = self.table.rowCount()
            self.table.insertRow(index)
            source = post.get("source") or {}
            values = (_when(post.get("at")), post.get("title") or (post.get("text") or "").split("\n")[0],
                      OUTCOMES.get(post.get("outcome"), post.get("outcome") or ""), source.get("label") or "",
                      str(post.get("status") or ""))
            for at, value in enumerate(values):
                item = _cell(value)
                if at == 0:
                    item.setData(Qt.UserRole, post.get("id"))
                self.table.setItem(index, at, item)
        self.search.apply()
        self._say_empty()
        self._picked()

    def _say_empty(self):
        self.note.say("" if self.posts else "Nothing was posted through this channel yet.")
        self.note.say_filtered("Nothing matches the search." if self.search.searching() else "")

    def _post(self):
        items = self.table.selectedItems()
        wanted = self.table.item(items[0].row(), 0).data(Qt.UserRole) if items else None
        return next((one for one in self.posts if one.get("id") == wanted), None)

    def _picked(self):
        post = self._post()
        self.again.setVisible(post is not None)
        if post is None:
            self.shown.setHtml("<p><i>Choose a post to see what it said.</i></p>")
            return
        said = card_html(post.get("title") or "", post.get("text") or "", "", [])
        facts = ["%s, HTTP %s" % (OUTCOMES.get(post.get("outcome"), ""), post.get("status") or "-")]
        if post.get("error"):
            facts.append(post["error"])
        if post.get("approved_by"):
            facts.append("approved by %s" % post["approved_by"])
        if post.get("mentions"):
            facts.append("mentioned " + ", ".join(post["mentions"]))
        self.shown.setHtml(said + "<p><small>%s</small></p>" % html.escape(" - ".join(facts)))

    def _again(self):
        post = self._post()
        if post is not None and widgets.confirm(self, "Send again", "Post these words again?",
                                                "It is a new post; the one before stays.", agree="Send"):
            self.again_wanted.emit(post["id"])


class ConnectionsTab(QWidget):
    HEADERS = ("Name", "Posts to", "Webhook", "Agents", "Cycles")
    save_wanted = Signal(dict, str)
    delete_wanted = Signal(str)
    test_wanted = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.connections, self.editing, self.current = [], False, None
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 14, 0, 0)
        column.setSpacing(8)
        panel = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        self.table = _table(self.HEADERS)
        self.table.setObjectName("teamsConnections")
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for index, width in ((1, 110), (2, 220), (3, 70), (4, 70)):
            self.table.setColumnWidth(index, width)
        self.table.itemSelectionChanged.connect(self._picked)
        self.search = widgets.SearchRow(self.table)
        self.note = widgets.empty_note(self.table, "")
        panel.layout().addWidget(self.table)
        column.addWidget(panel, 2)
        form = QFormLayout()
        self.name = QLineEdit()
        self.target = widgets.capped(QComboBox(), 200)
        for label, value in TARGETS:
            self.target.addItem(label, value)
        self.about = QLineEdit()
        self.webhook = QLineEdit()
        self.webhook.setEchoMode(QLineEdit.Password)
        self.title = QLineEdit()
        self.footer = QLineEdit()
        self.aliases = QPlainTextEdit()
        self.aliases.setFixedHeight(70)
        self.aliases.setPlaceholderText("name = sign-in address, one a line")
        self.agents = widgets.Segmented([label for label, _ in AGENTS], "Ask")
        self.cycles = widgets.Segmented([label for label, _ in CYCLES], "Auto")
        for label, widget in (("Name", self.name), ("Posts to", self.target), ("About", self.about),
                              ("Webhook", self.webhook), ("Default title", self.title), ("Footer", self.footer),
                              ("Mention by", self.aliases), ("Agents", self.agents), ("Cycles", self.cycles)):
            form.addRow(label, widget)
        self.agents.setToolTip("What agents may do through this channel. Ask: each post waits for your Approve. "
                               "An agent's own switch can only narrow it.")
        self.cycles.setToolTip("Whether cycle steps may post through this channel.")
        holder = QWidget()
        holder.setLayout(form)
        # The form scrolls: nine fields under the list are taller than a laptop's
        # screen leaves, and must not hold the window tall.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(holder)
        column.addWidget(scroll, 3)
        self.add = QPushButton("Add channel")
        self.add.clicked.connect(self._add)
        self.edit = QPushButton("Edit")
        self.edit.clicked.connect(lambda: self._editing(True))
        self.cancel = QPushButton("Cancel")
        self.cancel.clicked.connect(lambda: (self._editing(False), self._picked()))
        self.save = QPushButton("Save")
        self.save.setProperty("variant", "primary")
        self.save.clicked.connect(self._save)
        self.delete = QPushButton("Delete")
        self.delete.clicked.connect(self._delete)
        self.test = QPushButton("Test post")
        self.test.setToolTip("A real post to the channel, saying it is a test.")
        self.test.clicked.connect(self._test)
        self.message = QLabel("")
        self.message.setProperty("role", "hint")
        self.message.setWordWrap(True)
        column.addWidget(widgets.row(self.add, self.test, self.message, None, self.delete, self.cancel, self.edit,
                                     self.save))
        widgets.see_through(self)
        self.set_connections([])

    def say(self, text):
        self.message.setText(text or "")

    def set_connections(self, connections, pick=""):
        self.connections = list(connections or [])
        self.table.setRowCount(0)
        chosen = None
        for one in self.connections:
            index = self.table.rowCount()
            self.table.insertRow(index)
            access = (one.get("settings") or {}).get("access") or {}
            values = (one.get("name"), _words(TARGETS, one.get("target")),
                      one.get("webhook_host") or ("set" if one.get("token_set") else "not set"),
                      _words(AGENTS, access.get("agents")), _words(CYCLES, access.get("cycles")))
            for at, value in enumerate(values):
                item = _cell(value)
                if at == 0:
                    item.setData(Qt.UserRole, one.get("id"))
                self.table.setItem(index, at, item)
            if one.get("id") == (pick or (self.current or {}).get("id")):
                chosen = index
        self.note.say("" if self.connections else
                      "No Teams channel yet. Add one with the URL a Teams workflow gives "
                      "(\"Post to a channel when a webhook request is received\").")
        if chosen is not None:
            self.table.selectRow(chosen)
        elif self.connections:
            self.table.selectRow(0)
        self._editing(False)
        self._picked()

    def _picked(self):
        items = self.table.selectedItems()
        wanted = self.table.item(items[0].row(), 0).data(Qt.UserRole) if items else None
        self.current = next((one for one in self.connections if one.get("id") == wanted), None)
        self._fill(self.current or {})

    def _fill(self, one):
        settings = one.get("settings") or {}
        access = settings.get("access") or {"agents": "approve", "cycles": "post"}
        self.name.setText(one.get("name") or "")
        self.target.setCurrentIndex(max(0, self.target.findData(one.get("target") or "channel")))
        self.about.setText(one.get("about") or "")
        self.webhook.clear()
        self.webhook.setPlaceholderText(
            ("Set - %s. Type a new one to change it." % one["webhook_host"]) if one.get("webhook_host")
            else "https://... from the Teams workflow")
        self.title.setText(settings.get("title") or "")
        self.footer.setText(settings.get("footer") or "")
        self.aliases.setPlainText("\n".join("%s = %s" % pair for pair in sorted((settings.get("aliases") or {}).items())))
        self.agents.set_current(_words(AGENTS, access.get("agents")), notify=False)
        self.cycles.set_current(_words(CYCLES, access.get("cycles")), notify=False)
        self._sync_buttons()

    def _editing(self, on):
        self.editing = bool(on)
        for widget in (self.name, self.target, self.about, self.webhook, self.title, self.footer, self.aliases,
                       self.agents, self.cycles):
            widget.setEnabled(self.editing)
        self._sync_buttons()

    def _sync_buttons(self):
        chosen = self.current is not None
        self.edit.setVisible(chosen and not self.editing)
        self.save.setVisible(self.editing)
        self.cancel.setVisible(self.editing)
        self.delete.setVisible(chosen and not self.editing)
        self.test.setVisible(chosen and not self.editing and bool((self.current or {}).get("token_set")))
        self.add.setVisible(not self.editing)

    def _add(self):
        self.table.clearSelection()
        self.current = None
        self._fill({})
        self._editing(True)
        self.name.setFocus()

    def entry(self):
        aliases = {}
        for line in self.aliases.toPlainText().splitlines():
            if "=" in line:
                name, upn = (part.strip() for part in line.split("=", 1))
                if name and upn:
                    aliases[name] = upn
        made = dict(self.current or {}, provider=PROVIDER, name=self.name.text().strip(),
                    target=self.target.currentData(), about=self.about.text().strip())
        for runtime in ("token_set", "webhook_host"):
            made.pop(runtime, None)
        made["settings"] = dict((self.current or {}).get("settings") or {}, title=self.title.text().strip(),
                                footer=self.footer.text().strip(), aliases=aliases, access={
                                    "agents": dict(AGENTS)[self.agents.current()],
                                    "cycles": dict(CYCLES)[self.cycles.current()]})
        return made

    def has_unsaved(self):
        return self.editing

    def _save(self):
        self.save_wanted.emit(self.entry(), self.webhook.text().strip())

    def _delete(self):
        if self.current and widgets.confirm(self, "Delete channel", "Delete %s?" % self.current["name"],
                                            "Its history stays; cycles naming it stop posting.", agree="Delete"):
            self.delete_wanted.emit(self.current["id"])

    def _test(self):
        if self.current and widgets.confirm(self, "Test post", "Post a test to %s?" % self.current["name"],
                                            "It is a real post, saying it is a test.", agree="Send"):
            self.test_wanted.emit(self.current["id"])


class TeamsView(QWidget):
    back_requested = Signal()
    connections_changed = Signal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.core = None
        self.connections = []
        self._listed = self._listing = self._closing = False
        self._asked, self._pending = {}, {}
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
        header.addWidget(widgets.heading("Teams"))
        header.addStretch(1)
        caption = QLabel("Channel")
        caption.setProperty("role", "hint")
        header.addWidget(caption)
        self.picker = widgets.capped(QComboBox(), 260)
        self.picker.setObjectName("teamsPicker")
        self.picker.currentIndexChanged.connect(self._picked)
        header.addWidget(self.picker)
        outer.addLayout(header)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("teamsTabs")
        self.post_tab = NewPostTab()
        self.post_tab.send_wanted.connect(self.post)
        self.history = HistoryTab()
        self.history.again_wanted.connect(self.again)
        self.connections_tab = ConnectionsTab()
        self.connections_tab.save_wanted.connect(self.save_connection)
        self.connections_tab.delete_wanted.connect(self.delete_connection)
        self.connections_tab.test_wanted.connect(self.test_post)
        self.tabs.addTab(self.post_tab, "New post")
        self.tabs.addTab(self.history, "History")
        self.tabs.addTab(self.connections_tab, "Connections")
        self.tabs.currentChanged.connect(lambda _index: self._tab_changed())
        outer.addWidget(self.tabs, 1)

    # -- the integration window contract ----------------------------------------------
    def set_core(self, core):
        if core is self.core:
            return
        self._cancel_reads()
        self.core = core
        self._listed = self._listing = False
        self.connections = []
        self.picker.clear()
        self.post_tab.clear()
        self.history.set_posts([])
        self.connections_tab.set_connections([])
        if self.isVisible():
            self.list_connections()

    def has_unsaved(self):
        return self.post_tab.has_draft() or self.connections_tab.has_unsaved()

    def busy_reason(self, include_dialogs=True):
        if any(kind in WRITES for kind in self._pending.values()):
            return "A post or a channel is being saved in Teams. Wait for it to finish."
        if include_dialogs and any(dialog.isVisible() for dialog in self.findChildren(QDialog)):
            return "Finish or cancel the open Teams dialog first."
        return ""

    def shutdown(self):
        self._closing = True
        self._cancel_reads()
        for dialog in self.findChildren(QDialog):
            dialog.reject()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._closing and not self._listed:
            self.list_connections()

    def search_rows(self):
        found = [search.hit(one.get("name"), "Teams channel", "", target=("connection", one.get("id")))
                 for one in self.connections]
        for post in self.history.posts:
            found.append(search.hit(post.get("title") or (post.get("text") or "")[:60], "Teams post", "",
                                    keys=(post.get("text") or "")[:300], target=("post", post.get("id"))))
        return found

    def search_source(self):
        return None                          # a webhook cannot be searched

    def show_found(self, target):
        if not target:
            return False
        if target[0] == "connection" and any(one.get("id") == target[1] for one in self.connections):
            self.tabs.setCurrentWidget(self.connections_tab)
            self.connections_tab.set_connections(self.connections, pick=target[1])
            return True
        if target[0] == "post":
            self.tabs.setCurrentWidget(self.history)
            return True
        return False

    # -- reading and asking -----------------------------------------------------------
    def _current(self):
        return next((one for one in self.connections if one.get("id") == self.picker.currentData()), None)

    def list_connections(self, then=None):
        if self.core is None or self._listing:
            return
        self._listing = True

        def done(payload):
            self._listing, self._listed = False, True
            if payload.get("ok"):
                self._set_connections([one for one in payload.get("connections") or []
                                       if one.get("provider") == PROVIDER])
            else:
                self.connections_tab.say(payload.get("error") or "The channels could not be read.")
            if then is not None:
                then()
        self._call("list", lambda core, cancel: core.integrations("list", cancel=cancel), done)

    def _set_connections(self, connections, pick=""):
        wanted = pick or self.picker.currentData()
        self.connections = list(connections)
        self.picker.blockSignals(True)
        self.picker.clear()
        for one in self.connections:
            self.picker.addItem(one.get("name") or one.get("id"), one.get("id"))
        index = self.picker.findData(wanted)
        self.picker.setCurrentIndex(index if index >= 0 else (0 if self.connections else -1))
        self.picker.blockSignals(False)
        self.connections_tab.set_connections(self.connections, pick=pick)
        self._picked()
        self.connections_changed.emit()

    def _picked(self, _index=None):
        self.post_tab.set_connection(self._current() or {})
        self._tab_changed()

    # -- what others add ---------------------------------------------------------
    def add_tab(self, widget, title):
        """A tab from outside Teams - the posts agents wait to send (ADR-015 D6):
        after New post, before History, since it is about posts not sent yet.
        Teams does not know what it shows."""
        self.tabs.insertTab(1, widget, title)

    def set_tab_title(self, widget, title):
        index = self.tabs.indexOf(widget)
        if index >= 0 and self.tabs.tabText(index) != title:
            self.tabs.setTabText(index, title)

    def _tab_changed(self):
        if self.tabs.currentWidget() is self.history:
            self.read_history()

    def read_history(self):
        connection = self._current()
        if connection is None:
            self.history.set_posts([])
            return

        def done(payload):
            if payload.get("ok"):
                self.history.set_posts((payload.get("result") or {}).get("items") or [])
        self._call("history", lambda core, cancel: core.integrations(
            "call", connection["id"], "history", document={"size": 200}, cancel=cancel), done)

    def post(self, document):
        connection = self._current()
        if connection is None:
            return
        self.post_tab.send.setEnabled(False)
        self.post_tab.say("Sending...")

        def done(payload):
            if payload.get("ok"):
                made = (payload.get("result") or {}).get("post") or {}
                self.post_tab.clear()
                self.post_tab.say("Posted to %s - %s (HTTP %s). Accepted is not yet seen in the channel: "
                                  "the workflow posts it." % (connection["name"], OUTCOMES.get(made.get(
                                      "outcome"), ""), made.get("status") or "-"))
            else:
                self.post_tab.say(payload.get("error") or "The post was not sent.")
            self.post_tab._sync()
        self._call("post", lambda core, cancel: core.integrations(
            "call", connection["id"], "post", document=document), done)

    def again(self, post_id):
        connection = self._current()
        if connection is None:
            return

        def done(payload):
            if not payload.get("ok"):
                widgets.warn(self, "Send again", "Nothing was sent.", payload.get("error") or "")
            self.read_history()
        self._call("again", lambda core, cancel: core.integrations(
            "call", connection["id"], "again", document={"id": post_id}), done)

    def save_connection(self, entry, webhook):
        def done(payload):
            if payload.get("ok"):
                saved = (payload.get("connection") or {}).get("id") or ""
                self.connections_tab.say("Saved.")
                self.list_connections(then=lambda: self._set_connections(self.connections, pick=saved))
            else:
                self.connections_tab.say(payload.get("error") or "The channel was not saved.")
        document = {"connection": entry}
        if webhook:
            document["token"] = webhook
        self._call("save", lambda core, cancel: core.integrations("save", document=document), done)

    def delete_connection(self, connection_id):
        def done(payload):
            self.connections_tab.say("" if payload.get("ok") else payload.get("error") or "Not deleted.")
            self.list_connections()
        self._call("delete", lambda core, cancel: core.integrations("delete", connection_id), done)

    def test_post(self, connection_id):
        def done(payload):
            self.connections_tab.say("Test post sent - HTTP %s." % ((payload.get("result") or {}).get("post") or {})
                                     .get("status") if payload.get("ok") else
                                     payload.get("error") or "The test post was not sent.")
        self._call("test", lambda core, cancel: core.integrations("call", connection_id, "test"), done)

    def _cancel_reads(self):
        for cancel, kind in list(self._pending.items()):
            if kind not in WRITES:
                cancel.set()
                del self._pending[cancel]
        self._asked.clear()

    def _call(self, kind, work, done):
        """``work(core, cancel)`` on a thread; the latest answer of a kind reaches ``done``."""
        if self._closing or self.core is None:
            return
        self._asked[kind] = self._asked.get(kind, 0) + 1
        mine, core, cancel = self._asked[kind], self.core, threading.Event()
        self._pending[cancel] = kind

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
            if kind not in WRITES and cancel.is_set():
                return {"ok": False, "kind": "cancelled"}
            return work(core, None if kind in WRITES else cancel)
        if background.offloads(core):
            background.run(request, answer, owner=self)
        else:
            answer(*background.call(request))
