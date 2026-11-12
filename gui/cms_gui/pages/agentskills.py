"""Skills under Integrations -> Agents: the library, adding to it, choosing from it.

A skill is a folder of instructions an agent can be handed for a conversation -
what it is for, how to do it, and whatever files that takes. The library is
QAVector's, the same in every project.

**Nothing is added unseen.** A skill's instructions are read by an agent and its
scripts may be run by one, so adding is two steps on purpose: *Look* fetches it
and shows what is there - every file, where it came from, at which commit - and
*Add* takes exactly what was shown. Updating is the same two steps: what
changed is said first.

Three pieces live here: the **Skills tab** (the library), the **Add skill**
dialog, and the **panel** the composer opens to choose skills for a
conversation.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QDialog, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from .. import theme, widgets
from .agentwords import folder_text

LINK, FOLDER = "From a link", "From a folder"


# -- saying things --------------------------------------------------------------
def source_text(source):
    """Where a skill came from, short enough for a column."""
    source = source or {}
    if source.get("kind") == "git":
        url = str(source.get("url") or "")
        for prefix in ("https://", "ssh://", "file://"):
            if url.startswith(prefix):
                url = url[len(prefix):]
        url = url[:-4] if url.endswith(".git") else url
        return "%s · %s" % (url, source["path"]) if source.get("path") else url
    if source.get("kind") == "folder":
        return folder_text(source.get("folder"))
    return ""


def version_text(source):
    """Which copy it is: the branch and the commit, as git abbreviates one."""
    source = source or {}
    commit = str(source.get("commit") or "")[:7]
    if not commit:
        return "A folder's copy" if source.get("kind") == "folder" else ""
    return "%s @ %s" % (source["ref"], commit) if source.get("ref") else commit


def size_text(count):
    try:
        count = int(count)
    except (TypeError, ValueError):
        return ""
    if count < 1024:
        return "%d B" % count
    if count < 1024 * 1024:
        return "%.0f KB" % (count / 1024.0)
    return "%.1f MB" % (count / (1024.0 * 1024))


def files_text(skill):
    """How much a skill holds, and whether any of it could be run."""
    files = skill.get("files") or []
    said = "%d file%s · %s" % (len(files), "" if len(files) == 1 else "s",
                               size_text(skill.get("bytes")))
    return said + " · has scripts" if skill.get("scripts") else said


def changes_text(looked):
    """What a second look found different, for somebody to read before taking it."""
    changes = looked.get("changes") or {}
    lines = []
    for word, key in (("Changed", "changed"), ("Added", "added"), ("Removed", "removed")):
        lines.extend("%s  %s" % (word.ljust(8), path) for path in changes.get(key) or [])
    return "\n".join(lines)


#: How much of what a skill is for fits beside its name in the chooser.
NOTE_CHARS = 34


def _short(text, limit=NOTE_CHARS):
    """``text`` on one line, cut to ``limit`` characters."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def _table(headers):
    table = widgets.Table(0, len(headers), reclick_clears=False)
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setShowGrid(False)
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


# -- the library --------------------------------------------------------------------
class SkillsTab(QWidget):
    """The skills library: what is in it, where each came from, and at which
    commit - with adding, updating and removing."""

    HEADERS = ("Name", "What it is for", "Source", "Version", "Added")

    #: The tab wants its rows: a folder the core reads; nothing is fetched.
    wanted = Signal()
    #: (where to look, done(payload)) - fetches, and adds nothing
    look_requested = Signal(dict, object)
    #: ({"token", "picks", "replace"}, done(payload))
    add_requested = Signal(dict, object)
    #: (name, done(payload)) - looks at its source again
    update_requested = Signal(str, object)
    remove_requested = Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.skills, self.own = [], {}
        self.dialog = None
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 14, 0, 0)
        column.setSpacing(8)
        column.addWidget(widgets.lede(
            "A skill is a folder of instructions an agent can be handed for a "
            "conversation. Nothing is added unseen: Look shows every file and where it "
            "came from, and Add takes exactly that. Nothing updates by itself."))
        panel = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        self.table = _table(self.HEADERS)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        for index, width in ((0, 170), (2, 280), (3, 130), (4, 100)):
            self.table.setColumnWidth(index, width)
        self.table.itemSelectionChanged.connect(self._sync)
        self.search = widgets.SearchRow(self.table)
        self.search.changed.connect(self._say_empty)
        self.note = widgets.empty_note(self.table, "No skills yet - + Skill adds one from a "
                                                   "link or a folder.")
        self.skeleton = widgets.Skeleton(self.table, "rows")
        panel.layout().addWidget(self.table)
        column.addWidget(panel, 1)
        self.own_line = QLabel("")
        self.own_line.setProperty("role", "hint")
        self.own_line.setWordWrap(True)
        self.own_line.setVisible(False)
        column.addWidget(self.own_line)
        self.add_button = QPushButton("+ Skill")
        self.add_button.clicked.connect(self.add)
        self.update_button = QPushButton("Update…")
        self.update_button.setToolTip("Look at its source again and say what changed")
        self.update_button.clicked.connect(self.update_selected)
        self.remove_button = QPushButton("Remove…")
        self.remove_button.clicked.connect(self.remove)
        self.message = QLabel("")
        self.message.setProperty("role", "muted")
        column.addWidget(widgets.row(self.add_button, self.update_button, self.remove_button,
                                     self.message, None))
        self._sync()

    def shown(self):
        self.wanted.emit()

    def say(self, text):
        self.message.setText(text or "")

    def set_skills(self, skills, own=None):
        self.skills = list(skills or [])
        self.own = dict(own or {})
        chosen = self.selected()
        chosen_name = chosen.get("name") if chosen else ""
        self.table.setRowCount(0)
        for one in self.skills:
            row = self.table.rowCount()
            self.table.insertRow(row)
            source = one.get("source") or {}
            values = (one.get("name"), one.get("problem") or one.get("description"),
                      source_text(source), version_text(source),
                      str(source.get("added") or "")[:10])
            for column, value in enumerate(values):
                item = _cell(value)
                if column == 0:
                    item.setData(Qt.UserRole, one.get("name"))
                    item.setToolTip(files_text(one))
                if column == 1:
                    item.setToolTip(str(one.get("problem") or one.get("description") or ""))
                    if one.get("problem"):
                        # What is wrong reads as wrong: it is why the row is there.
                        item.setForeground(QColor(theme.BAD_INK))
                if column == 2:
                    item.setToolTip(str(source.get("url") or source.get("folder") or ""))
                if column == 3:
                    item.setToolTip(str(source.get("commit") or ""))
                self.table.setItem(row, column, item)
            if one.get("name") == chosen_name:
                self.table.selectRow(row)
        names = sorted({name for found in self.own.values() for name in found or []})
        self.own_line.setVisible(bool(names))
        self.own_line.setText("An agent's program has these by itself, in every turn, and "
                              "they are not kept here: %s." % ", ".join(names))
        self.search.apply()
        self._sync()

    def selected(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        wanted = item.data(Qt.UserRole) if item is not None else ""
        return next((one for one in self.skills if one.get("name") == wanted), None)

    def select(self, name):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.data(Qt.UserRole) == name:
                self.table.selectRow(row)
                return True
        return False

    def _sync(self):
        chosen = self.selected()
        self.remove_button.setEnabled(chosen is not None)
        # Looking again needs to know where it came from.
        self.update_button.setEnabled(bool(chosen and chosen.get("source")))

    def _say_empty(self):
        self.note.say_filtered("No skill matches the search." if self.search.searching() else "")

    # -- doing ----------------------------------------------------------------
    def add(self):
        dialog = AddSkillDialog(self)
        dialog.look_requested.connect(self.look_requested)
        dialog.add_requested.connect(self.add_requested)
        self.dialog = dialog
        dialog.exec()
        self.dialog = None
        dialog.deleteLater()

    def update_selected(self):
        chosen = self.selected()
        if chosen is None:
            return
        name = chosen["name"]
        self.update_button.setEnabled(False)
        self.say("Looking at where %s came from…" % name)

        def looked(payload):
            self._sync()
            if not payload.get("ok"):
                self.say(payload.get("error") or "Its source could not be looked at.")
                return
            changed = changes_text(payload)
            now = version_text(payload.get("source"))
            if not changed:
                self.say("%s is as its source is now%s." % (name, " - %s" % now if now else ""))
                return
            self.say("")
            before = version_text(dict(payload.get("source") or {}, commit=payload.get("was")))
            if not widgets.confirm_output(
                    self, "Update skill", "Take what changed in %s?" % name,
                    "%s. Read what changed: an agent follows these instructions and may "
                    "run these files." % (" -> ".join(part for part in (before, now) if part)
                                          or "Its folder changed"),
                    text=changed, agree="Update"):
                return

            def added(answer):
                self.say("%s updated." % name if answer.get("ok")
                         else answer.get("error") or "Not updated.")

            self.add_requested.emit({"token": payload.get("token"), "picks": [""],
                                     "replace": True}, added)

        self.update_requested.emit(name, looked)

    def remove(self):
        chosen = self.selected()
        if chosen is None:
            return
        if not widgets.confirm(
                self, "Remove skill", "Remove %s?" % chosen["name"],
                "Conversations that chose it will be told it is gone when they next "
                "take a turn. Where it came from is left alone.",
                agree="Remove", kind="warn"):
            return

        def done(payload):
            self.say("" if payload.get("ok") else payload.get("error") or "Not removed.")

        self.remove_requested.emit(chosen["name"], done)


class AddSkillDialog(QDialog):
    """Look at a skill, then add it: two steps, and the second takes exactly
    what the first showed."""

    look_requested = Signal(dict, object)
    add_requested = Signal(dict, object)

    HEADERS = ("Skill", "What it is for", "Holds", "Note")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._looked = {}
        self.setWindowTitle("Add a skill")
        self.frame = widgets.dress(self)
        self.setMinimumWidth(680)
        column = QVBoxLayout(self)
        column.setSpacing(8)
        self.kind = widgets.Segmented([LINK, FOLDER], LINK)
        self.kind.changed.connect(self._kind_changed)
        column.addWidget(self.kind)
        self.where = QLineEdit()
        self.where.setProperty("mono", "true")
        self.where.returnPressed.connect(self._look)
        self.where.textChanged.connect(self._typed)
        self.browse = QPushButton("Browse…")
        self.browse.setProperty("variant", "ghost")
        self.browse.clicked.connect(self._browse)
        self.look = QPushButton("Look")
        self.look.clicked.connect(self._look)
        self.field = widgets.field("", widgets.row(self.where, self.browse, self.look), " ")
        self.hint = self.field.layout().itemAt(2).widget()
        column.addWidget(self.field)
        self.source = QLabel("")
        self.source.setProperty("role", "muted")
        self.source.setWordWrap(True)
        self.source.setTextFormat(Qt.PlainText)
        column.addWidget(self.source)
        panel = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        self.table = _table(self.HEADERS)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        # Holds says files, size and whether any could run: the part a person
        # most needs whole. The note is short, and whole in its tooltip.
        for index, width in ((0, 150), (2, 210), (3, 110)):
            self.table.setColumnWidth(index, width)
        self.table.setMinimumHeight(150)
        self.table.itemSelectionChanged.connect(self._show_files)
        self.table.itemChanged.connect(lambda _item: self._sync())
        self.search = widgets.SearchRow(self.table)
        self.note = widgets.empty_note(self.table, "Nothing looked at yet. Look fetches it "
                                                   "and shows what is there; it adds nothing.")
        panel.layout().addWidget(self.table)
        column.addWidget(panel, 1)
        self.files = QPlainTextEdit()
        self.files.setReadOnly(True)
        self.files.setProperty("mono", "true")
        self.files.setFixedHeight(96)
        self.files.setPlaceholderText("The files of the skill picked above")
        column.addWidget(widgets.field("Files", self.files))
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
        self.add = QPushButton("Add")
        self.add.setProperty("variant", "primary")
        self.add.clicked.connect(self._add)
        buttons.addWidget(self.add)
        column.addLayout(buttons)
        self._kind_changed()
        self._sync()                    # nothing looked at: nothing to add

    # -- where to look -----------------------------------------------------------
    def _kind_changed(self, *_args):
        link = self.kind.current() == LINK
        self.browse.setVisible(not link)
        self.where.setPlaceholderText(
            "https://git.example.invalid/owner/repository/tree/main/skills/name" if link
            else "A folder with a SKILL.md, or with skills in it")
        self.hint.setText(
            "A repository, or a folder in one, as its page's address says it. Fetched "
            "with git and your own credentials; nothing of yours is sent." if link
            else "Copied from this computer. The folder itself is left as it is.")
        self._typed()

    def _typed(self, *_args):
        self.look.setEnabled(bool(self.where.text().strip()))
        self._say([])

    def _browse(self):
        chosen = widgets.pick_path(self, "A skill's folder", self.where.text().strip() or "~",
                                   directory=True)
        if chosen:
            self.where.setText(chosen)

    def request(self):
        text = self.where.text().strip()
        return {"url": text} if self.kind.current() == LINK else {"folder": text}

    def _look(self):
        if not self.where.text().strip() or not self.look.isEnabled():
            return
        self.look.setEnabled(False)
        self.look.setText("Looking…")
        self._say([])

        def done(payload):
            self.look.setText("Look")
            self.look.setEnabled(True)
            if not payload.get("ok"):
                self._show({})
                self._say([payload.get("error") or "It could not be looked at."])
                return
            self._show(payload)

        self.look_requested.emit(self.request(), done)

    # -- what was found ------------------------------------------------------------
    def _show(self, looked):
        self._looked = dict(looked or {})
        candidates = self._looked.get("candidates") or []
        source = self._looked.get("source") or {}
        self.source.setText(" · ".join(part for part in (
            version_text(source), source.get("url") or folder_text(source.get("folder")))
            if part))
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        usable = [one for one in candidates if not one.get("problems")]
        for one in candidates:
            row = self.table.rowCount()
            self.table.insertRow(row)
            problems = one.get("problems") or []
            name = _cell(one.get("name"))
            name.setData(Qt.UserRole, one.get("pick"))
            if problems:
                # Shown, with what is wrong - and not something to tick.
                name.setFlags(name.flags() & ~Qt.ItemIsEnabled & ~Qt.ItemIsUserCheckable)
            else:
                name.setFlags(name.flags() | Qt.ItemIsUserCheckable)
                # The one thing found is what was asked for; among several, and
                # for one the library already has, a person says which.
                name.setCheckState(Qt.Checked if len(usable) == 1 and not one.get("installed")
                                   else Qt.Unchecked)
            note = (problems[0] if problems
                    else "In the library: adding replaces it" if one.get("installed") else "")
            values = (name, _cell(one.get("description")),
                      _cell(files_text(one) if not problems else ""), _cell(note))
            for column, item in enumerate(values):
                if column in (1, 3):
                    item.setToolTip(item.text())
                self.table.setItem(row, column, item)
        self.table.blockSignals(False)
        self.search.apply()
        if candidates:
            self.table.selectRow(0)
        self._show_files()
        self._sync()

    def _candidate(self, row):
        item = self.table.item(row, 0)
        pick = item.data(Qt.UserRole) if item is not None else None
        return next((one for one in self._looked.get("candidates") or []
                     if one.get("pick") == pick), None)

    def _show_files(self):
        rows = self.table.selectionModel().selectedRows()
        candidate = self._candidate(rows[0].row()) if rows else None
        self.files.setPlainText("\n".join((candidate or {}).get("files") or []))

    def picks(self):
        return [self.table.item(row, 0).data(Qt.UserRole)
                for row in range(self.table.rowCount())
                if self.table.item(row, 0).checkState() == Qt.Checked]

    def _sync(self):
        self.add.setEnabled(bool(self.picks()))

    def _add(self):
        picks = self.picks()
        if not picks:
            return
        chosen = [one for one in self._looked.get("candidates") or []
                  if one.get("pick") in picks]
        self.add.setEnabled(False)
        self.add.setText("Adding…")

        def done(payload):
            self.add.setText("Add")
            self._sync()
            if payload.get("ok"):
                self.accept()
            else:
                self._say([payload.get("error") or "Not added."])

        self.add_requested.emit({"token": self._looked.get("token"), "picks": picks,
                                 "replace": any(one.get("installed") for one in chosen)}, done)

    def _say(self, problems):
        self.problem.setText("\n".join(one for one in problems if one))
        self.problem.setVisible(any(problems))


# -- choosing for a conversation -------------------------------------------------
class SkillsPanel(widgets.FloatingPanel):
    """The skills of the library, ticked for one conversation.

    A panel over the window rather than a popup - a popup takes the keyboard
    from the desktop for as long as it is open. It goes on Esc or a click
    elsewhere; what is ticked is the conversation's from that moment.
    """

    #: The names ticked, whenever a tick changes.
    changed = Signal(list)
    #: Somebody wants a skill the library does not have yet.
    add_requested = Signal()

    WIDTH = 520

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("role", "clarifypopup")
        self.setFixedWidth(self.WIDTH)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        head = widgets.row(widgets.kicker("Skills for this conversation"), None)
        head.setProperty("role", "clarifyhead")
        head.layout().setContentsMargins(12, 7, 12, 7)
        outer.addWidget(head)
        body = QWidget()
        column = QVBoxLayout(body)
        column.setContentsMargins(12, 10, 12, 10)
        column.setSpacing(6)
        self.list = widgets.CheckList(placeholder="Search skills…")
        self.list.set_noun("skills")        # the count reads "2 of 4 skills"
        # One line a skill, cut to the panel: what each is for in full is its
        # tooltip. Scrolling sideways in a list of names says nothing more.
        self.list.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.changed.connect(lambda: self.changed.emit(self.list.checked()))
        column.addWidget(self.list, 1)
        self.empty = QLabel("The library has no skills yet.")
        self.empty.setProperty("role", "hint")
        column.addWidget(self.empty)
        self.note = QLabel("")
        self.note.setProperty("role", "hint")
        self.note.setWordWrap(True)
        column.addWidget(self.note)
        self.add_button = QPushButton("Add skill…")
        self.add_button.setProperty("variant", "ghost")
        self.add_button.clicked.connect(self._add)
        closes = QLabel("Esc closes")
        closes.setProperty("role", "keys")
        column.addWidget(widgets.row(self.add_button, None, closes))
        outer.addWidget(body)

    def set_skills(self, skills, chosen=(), own=(), running=""):
        """Offer the library's skills with these ticked. ``own`` are the ones
        the agent has by itself; ``running`` says whether scripts can run."""
        self.list.blockSignals(True)
        self.list.clear()
        names = set()
        for one in skills or []:
            names.add(one.get("name"))
            said = one.get("problem") or one.get("description") or ""
            item = self.list.add(one.get("name"), note=_short(said),
                                 enabled=not one.get("problem"))
            item.setToolTip(said)
        # One chosen earlier and removed from the library since is still shown,
        # ticked, so it can be seen and unticked.
        for name in chosen or []:
            if name not in names:
                self.list.add(name, note="Not in the library any more")
        self.list.set_checked(list(chosen or []))
        self.list.blockSignals(False)
        self.empty.setVisible(not self.list.values())
        self.list.setVisible(bool(self.list.values()))
        self.set_note(own, running)

    def set_note(self, own=(), running=""):
        """What the list adds in words: the skills the agent has by itself,
        and whether the ones ticked can run what they carry."""
        lines = []
        if own:
            lines.append("The agent also has, in every turn: %s." % ", ".join(sorted(own)))
        if running:
            lines.append(running)
        self.note.setText(" ".join(lines))
        self.note.setVisible(bool(lines))

    def open_over(self, anchor):
        """Show it just above ``anchor`` - the composer sits at the foot of the
        window, so there is room over it and none under."""
        self.adjustSize()
        corner = anchor.mapToGlobal(anchor.rect().topLeft())
        screen = QApplication.screenAt(corner) or QApplication.primaryScreen()
        area = screen.availableGeometry() if screen is not None else None
        x, y = corner.x(), corner.y() - self.height() - 4
        if area is not None:
            x = max(area.left(), min(x, area.right() - self.width()))
            y = max(area.top(), y)
        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()
        self.list.search.setFocus()

    def keyPressEvent(self, event):          # noqa: N802 - Qt's own spelling
        if event.key() == Qt.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)

    def _add(self):
        self.hide()
        self.add_requested.emit()
