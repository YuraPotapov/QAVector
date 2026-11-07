"""Integrations -> Jira: one project's issues, connections, activity and settings.

**Lazy, by rule.** Nothing here asks Jira anything unless a person did something
that asks: the Issues tab shown for the first time since the application
started, a filter changed, Enter in the search box, Load more, an issue opened,
Refresh, Test. The list asks for the few fields a row shows; an opened issue is
read alone; a filter's list of projects or statuses is read the first time its
box opens. What was loaded stays on screen - with when it was loaded - until a
person asks again, and coming back to the page asks nothing.

**Through the core only.** The page names a connection and an operation; the
core reads the token and asks Jira (``integrations/cli.py``). A token never
reaches this process.
"""

import datetime
import re
import threading

from PySide6.QtCore import QEvent, QRect, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QColor, QDesktopServices, QFont, QFontMetrics, QKeySequence, QPen,
                           QShortcut)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QCompleter,
                               QDialog, QFrame,
                               QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMenu, QPlainTextEdit,
                               QPushButton, QScrollArea, QSizePolicy, QStackedWidget,
                               QStyle, QStyledItemDelegate, QTabWidget, QToolButton,
                               QVBoxLayout, QWidget)

from .. import background, core as core_mod, icons, search, theme, widgets
from .jiraconnections import ActivityTab, ConnectionsTab, SettingsTab
from .jiradialogs import split_labels
from .jiradoc import DescriptionEditor

#: Rows a page of the list asks for.
PAGE_ROWS = 25
#: Issues one search from Ctrl+K brings back from each connection.
FOUND_ROWS = 10
#: The tallest the description grows while it is edited; past it, it scrolls.
DESCRIPTION_HEIGHT = 420

#: Requests that change something - in Jira or in the project's files. They
#: are never cancelled, and leaving the project or the application waits for them.
WRITES = frozenset(("write", "save", "remove", "app-save"))

#: Comments an opened issue shows before "Show earlier".
COMMENTS = 20


#: A failure's kind (integrations/cli.py) -> (tag text, tag variant, what to do).
HEALTH = {
    "ok": ("Connected", "accent", ""),
    "signin": ("Sign-in refused", "bad",
               "Jira refused the sign-in. Check the email and token in Connections."),
    "forbidden": ("No access", "warn",
                  "The account may not do that. Ask a Jira administrator for access."),
    "unreachable": ("Unreachable", "bad",
                    "Jira could not be reached. Check the site and your network, "
                    "then Refresh."),
    "no_token": ("No token", "warn", "This connection has no token. Set it in Connections."),
    "busy": ("Busy", "warn", "Jira asked to slow down. Try again in a minute."),
}


# -- small things -------------------------------------------------------------
def parse_time(text):
    """A Jira timestamp - ``2026-11-05T10:00:00.000+0000`` - as an aware datetime."""
    match = re.match(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.\d+)?"
                     r"(Z|[+-]\d\d:?\d\d)?$", str(text or "").strip())
    if not match:
        return None
    parts = [int(one) for one in match.groups()[:6]]
    zone = match.group(7) or "Z"
    if zone == "Z":
        offset = datetime.timedelta(0)
    else:
        digits = zone.replace(":", "")
        offset = datetime.timedelta(hours=int(digits[1:3]), minutes=int(digits[3:5]))
        if digits[0] == "-":
            offset = -offset
    return datetime.datetime(*parts, tzinfo=datetime.timezone(offset))


def ago(when, now=None):
    """``when`` - a datetime or a Jira timestamp - the way a person says it."""
    moment = when if isinstance(when, datetime.datetime) else parse_time(when)
    if moment is None:
        return ""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    seconds = (now - moment).total_seconds()
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return "%d minute%s ago" % (minutes, "" if minutes == 1 else "s")
    hours = minutes // 60
    if hours < 24:
        return "%d hour%s ago" % (hours, "" if hours == 1 else "s")
    days = hours // 24
    if days == 1:
        return "yesterday"
    if days < 7:
        return "%d days ago" % days
    local = moment.astimezone()
    return "%d %s" % (local.day, local.strftime("%b")) + (
        "" if local.year == now.year else " %d" % local.year)


def initials(name):
    words = [one for one in re.split(r"[\s._-]+", str(name or "")) if one]
    if not words:
        return "?"
    letters = words[0][:1] + (words[-1][:1] if len(words) > 1 else words[0][1:2])
    return letters.upper()


def chip_colours(category):
    """(fill, ink) of a status chip, by the status's category, read from the theme."""
    if category == "indeterminate":
        return theme.MARK_TINT, theme.MARK_INK
    if category == "done":
        return theme.NEUTRAL[100], theme.OK
    return theme.NEUTRAL[200], theme.NEUTRAL[800]


def zone():
    """An empty zone whose title and detail can be changed: ``(frame, title, detail)``."""
    frame = widgets.empty_zone("", "")
    title, detail = frame.findChildren(QLabel)[:2]
    return frame, title, detail


def _label(text="", role=None, wrap=False):
    label = QLabel(str(text))
    label.setTextFormat(Qt.PlainText)
    label.setWordWrap(wrap)
    if role:
        label.setProperty("role", role)
    return label


class StatusChip(QLabel):
    """A status in its category's colours: what the list's rows paint, as a widget."""

    def __init__(self, text="", category=""):
        super().__init__(text)
        self._category = category
        self.set(text, category)

    def set(self, text, category):
        self._category = category
        self.setText(text)
        fill, ink = chip_colours(category)
        self.setStyleSheet("background: %s; color: %s; padding: 2px 6px; font-size: 11px;"
                           % (fill, ink))
        self.setVisible(bool(text))

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.StyleChange, QEvent.PaletteChange):
            fill, ink = chip_colours(self._category)
            self.setStyleSheet("background: %s; color: %s; padding: 2px 6px; "
                               "font-size: 11px;" % (fill, ink))


class Initials(QLabel):
    """A person as two letters on a square - the design's avatar, drawn not fetched."""

    def __init__(self, name=""):
        super().__init__()
        self.setProperty("role", "initials")
        self.setAlignment(Qt.AlignCenter)
        self.setFixedSize(24, 22)
        self.set(name)

    def set(self, name):
        self.setText(initials(name) if name else "")
        self.setVisible(bool(name))


# -- the list -----------------------------------------------------------------
class IssueDelegate(QStyledItemDelegate):
    """One issue as the list shows it: key and type, priority, summary, status,
    assignee. Painted from theme tokens read at paint time, for dark mode."""

    HEIGHT = 92

    def sizeHint(self, option, index):
        return QSize(320, self.HEIGHT)

    def paint(self, painter, option, index):
        row = index.data(Qt.UserRole)
        if not isinstance(row, dict):
            return
        rect = option.rect
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        painter.save()
        painter.fillRect(rect, QColor(theme.SELECT_BG if selected
                                      else theme.SECTION_BG if hovered else theme.BG))
        if selected:
            painter.fillRect(QRect(rect.left(), rect.top(), 3, rect.height()),
                             QColor(theme.ACCENT))
        painter.setPen(QColor(theme.DIVIDER))
        painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())
        left, right = rect.left() + 14, rect.right() - 12
        muted = QColor(theme.SELECT_MUTED if selected else theme.NEUTRAL[600])
        ink = QColor(theme.SELECT_INK if selected else theme.TEXT)

        small = QFont(option.font)
        small.setPixelSize(12)
        metrics = QFontMetrics(small)
        priority = str(row.get("priority") or "")
        priority_width = metrics.horizontalAdvance(priority) + 4 if priority else 0
        painter.setFont(small)
        painter.setPen(muted)
        painter.drawText(QRect(right - priority_width, rect.top() + 10, priority_width, 20),
                         Qt.AlignRight | Qt.AlignVCenter, priority)

        mono = theme.mono_font()
        mono.setPixelSize(11)
        painter.setFont(mono)
        head = "%s  ·  %s" % (row.get("key", ""), row.get("type", "")) if row.get("type") \
            else str(row.get("key", ""))
        width = max(40, right - left - priority_width - 10)
        painter.drawText(QRect(left, rect.top() + 10, width, 20), Qt.AlignLeft | Qt.AlignVCenter,
                         QFontMetrics(mono).elidedText(head, Qt.ElideRight, width))

        title = QFont(option.font)
        title.setPixelSize(13)
        painter.setFont(title)
        painter.setPen(ink)
        painter.drawText(QRect(left, rect.top() + 34, right - left, 22),
                         Qt.AlignLeft | Qt.AlignVCenter,
                         QFontMetrics(title).elidedText(str(row.get("summary") or ""),
                                                        Qt.ElideRight, right - left))

        chip = QFont(option.font)
        chip.setPixelSize(11)
        chip_metrics = QFontMetrics(chip)
        status = str(row.get("status") or "")
        assignee = str(row.get("assignee") or "Unassigned")
        assignee_width = min(140, metrics.horizontalAdvance(assignee) + 4)
        if status:
            text = chip_metrics.elidedText(status, Qt.ElideRight,
                                           max(30, right - left - assignee_width - 24))
            box = QRect(left, rect.top() + 61, chip_metrics.horizontalAdvance(text) + 12, 20)
            fill, chip_ink = chip_colours(row.get("category"))
            painter.fillRect(box, QColor(fill))
            painter.setFont(chip)
            painter.setPen(QColor(chip_ink))
            painter.drawText(box, Qt.AlignCenter, text)
        painter.setFont(small)
        painter.setPen(muted)
        painter.drawText(QRect(right - assignee_width, rect.top() + 61, assignee_width, 20),
                         Qt.AlignRight | Qt.AlignVCenter,
                         metrics.elidedText(assignee, Qt.ElideRight, assignee_width))
        if selected and option.state & QStyle.State_HasFocus:
            painter.setPen(QPen(QColor(theme.SELECT_INK), 1, Qt.DotLine))
            painter.drawRect(rect.adjusted(5, 3, -5, -3))
        painter.restore()


class LazyCombo(QComboBox):
    """A filter whose choices are read the first time its list is opened.

    Until then it holds what it was left showing - "All projects", or the one
    project picked last time - so a page that is only looked at never asks Jira
    which projects there are.
    """

    wanted = Signal()

    def __init__(self, everything):
        super().__init__()
        self._everything = everything
        self._loaded = False
        self._open_when_loaded = False
        self.addItem(everything, "")

    def loaded(self):
        return self._loaded

    def showPopup(self):
        if self._loaded:
            super().showPopup()
            return
        self._open_when_loaded = True
        if self.count() < 2 or self.itemData(self.count() - 1) != "\0loading":
            self.addItem("Loading…", "\0loading")
        self.wanted.emit()

    def set_choices(self, choices):
        """``[(text, value)]``; keeps the value showing if it is still among them."""
        current = self.value()
        self.blockSignals(True)
        self.clear()
        self.addItem(self._everything, "")
        for text, value in choices:
            self.addItem(text, value)
        index = self.findData(current)
        if index < 0 and current:
            self.addItem(current, current)
            index = self.count() - 1
        self.setCurrentIndex(max(0, index))
        self.blockSignals(False)
        self._loaded = True
        if self._open_when_loaded and self.isVisible():
            self._open_when_loaded = False
            super().showPopup()

    def failed(self):
        """The list could not be read: back to what it showed, ready to ask again."""
        self._open_when_loaded = False
        index = self.findData("\0loading")
        if index >= 0:
            self.removeItem(index)

    def value(self):
        data = self.currentData()
        return "" if data in (None, "\0loading") else str(data)

    def set_value(self, value, text=None):
        """Show ``value`` without reading the list - for what was left picked."""
        if not value:
            self.setCurrentIndex(0)
            return
        index = self.findData(value)
        if index < 0:
            self.addItem(text or value, value)
            index = self.count() - 1
        self.setCurrentIndex(index)

    def reset(self):
        self.blockSignals(True)
        self.clear()
        self.addItem(self._everything, "")
        self.blockSignals(False)
        self._loaded = False
        self._open_when_loaded = False


#: The picker's first entry: the assignee left as it was.
KEEP = "\0keep"
LOADING = "\0loading"


class PeoplePicker(QComboBox):
    """Who carries an issue, while it is edited.

    It starts with who carries it now, Unassigned and Me. The people it may be
    assigned to are read the first time the list is opened, or when a name is
    typed and Enter pressed, and never before. Typing filters whoever is
    already in the list without asking Jira.
    """

    #: A name to look for; "" for whoever Jira lists first.
    wanted = Signal(str)

    def __init__(self):
        super().__init__()
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.setAccessibleName("Assignee")
        self.lineEdit().setPlaceholderText("Type a name, then Enter")
        self.lineEdit().returnPressed.connect(self._typed)
        finder = self.completer()
        finder.setFilterMode(Qt.MatchContains)
        finder.setCaseSensitivity(Qt.CaseInsensitive)
        finder.setCompletionMode(QCompleter.PopupCompletion)
        self._current = ""
        self._loaded = False
        self._open_when_loaded = False

    def start(self, current):
        """Who carries the issue as editing begins. Nothing is read yet."""
        self._current = current or ""
        self._loaded = False
        self._open_when_loaded = False
        self.blockSignals(True)
        self.clear()
        self.addItem(current or "Unassigned", KEEP)
        if current:
            self.addItem("Unassigned", "")
        self.addItem("Me", "me")
        self.setCurrentIndex(0)
        self.blockSignals(False)

    def value(self):
        """None while it is left as it was; else "", "me" or an account id."""
        data = self.currentData()
        if data in (None, KEEP, LOADING):
            return None
        if data not in ("", "me") and self.itemText(self.currentIndex()) == self._current:
            return None
        return str(data)

    def showPopup(self):
        if self._loaded:
            super().showPopup()
            return
        self._ask("")

    def _typed(self):
        text = self.lineEdit().text().strip()
        index = self.findText(text, Qt.MatchFixedString)
        if index >= 0:
            self.setCurrentIndex(index)
        elif text:
            self._ask(text)

    def _ask(self, query):
        self._open_when_loaded = True
        if self.findData(LOADING) < 0:
            self.blockSignals(True)
            self.addItem("Loading…", LOADING)
            self.blockSignals(False)
        self.wanted.emit(query)

    def set_people(self, people):
        """``[{id, name}]`` from Jira, under the three it started with."""
        chosen = self.currentData()
        fixed = [(self.itemText(index), self.itemData(index)) for index in range(self.count())
                 if self.itemData(index) in (KEEP, "", "me")]
        self.blockSignals(True)
        self.clear()
        for text, data in fixed:
            self.addItem(text, data)
        seen = set()
        for one in people or []:
            if one.get("id") and one["id"] not in seen:
                seen.add(one["id"])
                self.addItem(one.get("name") or one["id"], one["id"])
        self.setCurrentIndex(max(0, self.findData(chosen)))
        self.blockSignals(False)
        self._loaded = True
        if self._open_when_loaded and self.isVisible():
            self._open_when_loaded = False
            super().showPopup()

    def failed(self):
        self._open_when_loaded = False
        index = self.findData(LOADING)
        if index >= 0:
            self.blockSignals(True)
            self.removeItem(index)
            self.blockSignals(False)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        # What was typed and not picked is not a choice.
        self.lineEdit().setText(self.itemText(self.currentIndex()))


class _Rich(QLabel):
    """Escaped HTML the core rendered - a description, a comment - as text to read.

    Only web links open, in the browser; nothing in it is loaded from anywhere.
    """

    def __init__(self):
        super().__init__()
        self.setTextFormat(Qt.RichText)
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
        self.setOpenExternalLinks(False)
        self.linkActivated.connect(_open_web)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)


def _open_web(url):
    if str(url).startswith(("https://", "http://")):
        QDesktopServices.openUrl(QUrl(url))


# -- one issue ----------------------------------------------------------------
class IssueDetail(QFrame):
    """The opened issue: what the core read of it, and what can be done to it.

    It is read-only until Edit is pressed, so a stray click or keystroke
    changes nothing. Edit turns summary, priority, labels and description into
    fields where they stand. Save sends only what changed, together with when
    the issue was last changed at the moment editing began. If someone changed
    it in Jira since, the edit is refused rather than written over theirs.
    """

    earlier_requested = Signal()
    #: A transition the person picked: ``{id, name, to}``.
    transition_requested = Signal(dict)
    #: Editing began on the issue shown: what may change is read now.
    edit_started = Signal()
    #: The request for ``edit``: ``{key, updated, fields}``.
    save_requested = Signal(dict)
    #: ``me``, or "" for nobody.
    assign_requested = Signal(str)
    reload_requested = Signal()
    attach_requested = Signal()
    #: An attachment to save: ``{id, filename, url, ...}``.
    download_requested = Signal(dict)
    #: A comment to post: a document, or on Server text.
    comment_requested = Signal(object)
    #: People to mention are wanted: "" for the first read, else a name.
    people_wanted = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("jiraDetail")
        self.setProperty("role", "panel-fixed")
        self.setMinimumWidth(320)
        self.issue = None
        #: The issue as it was when Edit was pressed; None when not editing.
        self.editing = None
        self._editable = None
        self._rewrite = False
        self._partial = False
        self._working = False
        frame = QVBoxLayout(self)
        frame.setContentsMargins(0, 0, 0, 0)
        frame.setSpacing(0)
        self.stack = QStackedWidget()
        frame.addWidget(self.stack, 1)

        empty = QWidget()
        column = QVBoxLayout(empty)
        column.setContentsMargins(18, 18, 18, 18)
        self.empty = widgets.empty_zone(
            "No issue selected",
            "Choose one on the left. Its details are read from Jira when you open it.")
        column.addWidget(self.empty)
        column.addStretch(1)
        self.stack.addWidget(empty)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QFrame()
        body.setObjectName("jiraDetailBody")
        self.body = body
        layout = QVBoxLayout(body)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        top = QHBoxLayout()
        self.key = widgets.elided_mono("", Qt.ElideRight)
        top.addWidget(self.key, 1)
        self.priority = widgets.Tag("", "neutral")
        top.addWidget(self.priority, 0, Qt.AlignRight)
        self.priority_edit = widgets.capped(QComboBox(), 200)
        self.priority_edit.setAccessibleName("Priority")
        self.priority_edit.currentIndexChanged.connect(self._edited)
        top.addWidget(self.priority_edit, 0, Qt.AlignRight)
        layout.addLayout(top)
        self.summary = _label("", "h2", wrap=True)
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.summary.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self.summary)
        self.summary_edit = QLineEdit()
        self.summary_edit.setObjectName("jiraSummaryEdit")
        self.summary_edit.setAccessibleName("Summary")
        self.summary_edit.textChanged.connect(self._edited)
        layout.addWidget(self.summary_edit)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.status = QToolButton()
        self.status.setObjectName("jiraStatus")
        self.status.setProperty("variant", "primary")
        self.status.setProperty("hasmenu", "true")
        self.status.setPopupMode(QToolButton.InstantPopup)
        self.status.setMenu(QMenu(self.status))
        self.status.setToolTip("Move it along its workflow")
        actions.addWidget(self.status)
        self.edit = QPushButton("Edit")
        icons.button(self.edit, "edit", "Edit")
        self.edit.setToolTip("Change summary, priority, labels or description here")
        self.edit.clicked.connect(self.begin_edit)
        actions.addWidget(self.edit)
        self.save = QPushButton("Save")
        self.save.setProperty("variant", "primary")
        self.save.setToolTip("Send what changed to Jira (Ctrl+S)")
        self.save.clicked.connect(self._save)
        actions.addWidget(self.save)
        self.cancel = QPushButton("Cancel")
        self.cancel.setToolTip("Put the changes down; nothing is sent")
        self.cancel.clicked.connect(self.put_down)
        actions.addWidget(self.cancel)
        self.more = QToolButton()
        icons.button(self.more, "more", "")
        self.more.setObjectName("jiraMore")
        self.more.setAccessibleName("More")
        self.more.setToolTip("More")
        self.more.setPopupMode(QToolButton.InstantPopup)
        self.more.setProperty("menuglyph", "false")
        menu = QMenu(self.more)
        menu.addAction("Open in Jira", self._open_in_jira)
        menu.addAction("Copy key", lambda: self._copy("key"))
        menu.addAction("Copy link", lambda: self._copy("url"))
        menu.addSeparator()
        self.assign_me = menu.addAction("Assign to me",
                                        lambda: self.assign_requested.emit("me"))
        self.unassign = menu.addAction("Unassign", lambda: self.assign_requested.emit(""))
        menu.addSeparator()
        menu.addAction("Reload", self.reload_requested)
        self.more.setMenu(menu)
        actions.addWidget(self.more)
        actions.addStretch(1)
        layout.addLayout(actions)
        # What went wrong with an edit, where the person is looking: under
        # Save, not past the comments.
        self.edit_problem = _label("", "error", wrap=True)
        self.edit_problem.setVisible(False)
        layout.addWidget(self.edit_problem)

        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(4)
        self.assignee_mark = Initials()
        self.assignee = _label("")
        self.reporter = _label("")
        self.project = _label("")
        self.labels = widgets.mono("")
        self.labels.setWordWrap(True)
        self.labels_edit = QLineEdit()
        self.labels_edit.setProperty("mono", "true")
        self.labels_edit.setAccessibleName("Labels")
        self.labels_edit.setPlaceholderText("Separated by spaces or commas")
        self.labels_edit.textChanged.connect(self._edited)
        labels = QVBoxLayout()
        labels.setSpacing(0)
        labels.addWidget(self.labels)
        labels.addWidget(self.labels_edit)
        assignee = QHBoxLayout()
        assignee.setSpacing(6)
        assignee.addWidget(self.assignee_mark)
        assignee.addWidget(self.assignee, 1)
        self.assignee_edit = widgets.capped(PeoplePicker(), 260)
        self.assignee_edit.currentIndexChanged.connect(self._edited)
        assignee.addWidget(self.assignee_edit, 1)
        grid.addWidget(_label("Assignee", "muted"), 0, 0)
        grid.addWidget(_label("Reporter", "muted"), 0, 1)
        grid.addLayout(assignee, 1, 0)
        grid.addWidget(self.reporter, 1, 1)
        grid.addWidget(_label("Project", "muted"), 2, 0)
        grid.addWidget(_label("Labels", "muted"), 2, 1)
        grid.addWidget(self.project, 3, 0)
        grid.addLayout(labels, 3, 1)
        grid.setRowMinimumHeight(2, 26)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        layout.addWidget(widgets.hline())
        layout.addWidget(widgets.kicker("Description"))
        self.description = _Rich()
        layout.addWidget(self.description)
        self.description_edit = QPlainTextEdit()
        self.description_edit.setObjectName("jiraDescriptionEdit")
        self.description_edit.setAccessibleName("Description")
        self.description_edit.setPlaceholderText("No description")
        self.description_edit.textChanged.connect(self._edited)
        self.description_edit.document().documentLayout().documentSizeChanged.connect(
            self._fit_description)
        layout.addWidget(self.description_edit)
        # Cloud: the description with its formatting. Server's is text.
        self.rich_edit = DescriptionEditor()
        self.rich_edit.changed.connect(self._edited)
        # Who may be mentioned is read once per issue, for both.
        self._people_of = None
        self._people = []
        self.rich_edit.people_wanted.connect(self._want_people)
        layout.addWidget(self.rich_edit)
        # A description with what the editor cannot hold - a table, a picture -
        # stays as it is unless the person chooses to edit it without them.
        rich = QHBoxLayout()
        rich.setSpacing(10)
        self.rich_note = _label("", "hint", wrap=True)
        rich.addWidget(self.rich_note, 1)
        self.rewrite = QPushButton("Rewrite as plain text")
        self.rewrite.setProperty("variant", "ghost")
        self.rewrite.clicked.connect(self._toggle_rewrite)
        rich.addWidget(self.rewrite, 0, Qt.AlignTop)
        self.rich_row = QFrame()
        self.rich_row.setObjectName("jiraRichNote")
        self.rich_row.setLayout(rich)
        rich.setContentsMargins(10, 6, 6, 6)
        layout.addWidget(self.rich_row)

        layout.addWidget(widgets.hline())
        files = QHBoxLayout()
        self.files_title = widgets.kicker("Attachments")
        files.addWidget(self.files_title, 1)
        self.attach = QPushButton("Add")
        icons.button(self.attach, "attach", "Add")
        self.attach.setToolTip("Attach a file from this computer")
        self.attach.clicked.connect(self.attach_requested)
        files.addWidget(self.attach)
        layout.addLayout(files)
        self.files = QVBoxLayout()
        self.files.setSpacing(4)
        layout.addLayout(self.files)

        layout.addWidget(widgets.hline())
        self.comments_title = widgets.kicker("Comments")
        layout.addWidget(self.comments_title)
        self.earlier = QPushButton("Show earlier comments")
        self.earlier.setProperty("variant", "ghost")
        self.earlier.clicked.connect(self.earlier_requested)
        layout.addWidget(self.earlier, 0, Qt.AlignLeft)
        self.comments = QVBoxLayout()
        self.comments.setSpacing(10)
        layout.addLayout(self.comments)

        self.composer = DescriptionEditor(name="Comment",
                                          placeholder="Add a comment… (@ mentions someone)",
                                          shortest=64)
        self.composer.changed.connect(self._composing)
        self.composer.people_wanted.connect(self._want_people)
        layout.addWidget(self.composer)
        bottom = QHBoxLayout()
        self.post = QPushButton("Post comment")
        self.post.setEnabled(False)
        self.post.clicked.connect(
            lambda: self.comment_requested.emit(self.composer.body()))
        bottom.addWidget(self.post)
        bottom.addStretch(1)
        layout.addLayout(bottom)
        self.problem = _label("", "error", wrap=True)
        self.problem.setVisible(False)
        layout.addWidget(self.problem)
        self.note = _label("", "muted", wrap=True)
        self.note.setVisible(False)
        layout.addWidget(self.note)
        layout.addStretch(1)
        self.scroll.setWidget(body)
        self.stack.addWidget(self.scroll)

        gone = QWidget()
        column = QVBoxLayout(gone)
        column.setContentsMargins(18, 18, 18, 18)
        self.gone, self.gone_title, self.gone_detail = zone()
        column.addWidget(self.gone)
        column.addStretch(1)
        self.stack.addWidget(gone)
        self.skeleton = widgets.Skeleton(self, "lines", ground="SURFACE")
        # On only while editing, so the keys mean nothing here otherwise.
        self.save_key = QShortcut(QKeySequence.Save, self, self._save)
        self.save_key.setContext(Qt.WidgetWithChildrenShortcut)
        self.escape_key = QShortcut(QKeySequence(Qt.Key_Escape), self, self._escape)
        self.escape_key.setContext(Qt.WidgetWithChildrenShortcut)
        self._apply_mode()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.StyleChange, QEvent.PaletteChange):
            icons.button(self.more, "more", "")
            icons.button(self.edit, "edit", "Edit")
            icons.button(self.attach, "attach", "Add")
            self.priority.set_variant("neutral")
            if self.issue is not None:
                self._fill_files(self.issue.get("attachments") or [], False)

    # -- what it shows --------------------------------------------------------
    def clear(self):
        self._stop_editing()
        self.issue = None
        self.skeleton.stop()
        self.stack.setCurrentIndex(0)

    def loading(self, row=None):
        """Show what the list already knows while the rest is read."""
        if self.issue is None or (row and self.issue.get("key") != row.get("key")):
            if row:
                self.show_issue(dict(row, description_html="", attachments=[],
                                     comments={"items": [], "total": 0},
                                     labels=[], project={}, reporter=""), partial=True)
            self.skeleton.start()

    def unavailable(self, key, text):
        self._stop_editing()
        self.skeleton.stop()
        self.issue = None
        self.gone_title.setText("%s is not available" % key)
        self.gone_detail.setText(text)
        self.stack.setCurrentIndex(2)

    def say(self, text):
        """What went wrong: under Save while editing, under the issue otherwise."""
        self.skeleton.stop()
        shown = self.edit_problem if self.editing is not None else self.problem
        shown.setText(text)
        shown.setVisible(bool(text))

    def show_issue(self, issue, partial=False):
        """An issue as the core read it. The one being edited may be read again
        - by Refresh, say - and what was typed stays; it is compared with the
        issue as it was when Edit was pressed."""
        if self.editing is not None and issue.get("key") != self.editing.get("key"):
            self._stop_editing()
        if issue.get("key") != self._people_of:
            self._people_of, self._people = issue.get("key"), []
            for editor in (self.composer, self.rich_edit):
                editor.forget_people()
        if not partial:
            self.skeleton.stop()
        self.issue = issue
        self._partial = partial
        self.problem.setVisible(False)
        self.key.setText("%s  /  %s" % (issue.get("key", ""), issue.get("type") or "Issue")
                         if issue.get("type") else issue.get("key", ""))
        priority = issue.get("priority") or ""
        self.priority.set("%s priority" % priority if priority else "", "neutral")
        self.summary.setText(issue.get("summary") or "")
        self.status.setText(issue.get("status") or "No status")
        self._fill_transitions(issue.get("transitions") or [])
        self.assignee.setText(issue.get("assignee") or "Unassigned")
        self.assignee_mark.set(issue.get("assignee") or "")
        self.reporter.setText(issue.get("reporter") or "—")
        project = issue.get("project") or {}
        self.project.setText(project.get("name") or project.get("key") or "—")
        self.labels.setText("  ·  ".join(issue.get("labels") or []) or "—")
        html = issue.get("description_html") or ""
        if html:
            self.description.setText(html)
            self.description.setProperty("role", "")
        else:
            self.description.setText("" if partial else "No description.")
            self.description.setProperty("role", "muted")
        self.description.style().unpolish(self.description)
        self.description.style().polish(self.description)
        self._fill_files(issue.get("attachments") or [], partial)
        self._fill_comments(issue.get("comments") or {}, partial)
        self._apply_mode()
        self.stack.setCurrentIndex(1)

    def _fill_transitions(self, offered):
        menu = self.status.menu()
        menu.clear()
        for one in offered:
            name, to = one.get("name") or "", one.get("to") or ""
            text = name if not to or to.lower() == name.lower() else "%s  (to %s)" % (name, to)
            menu.addAction(text, lambda chosen=dict(one): self.transition_requested.emit(chosen))
        self.status.setToolTip("Move it along its workflow" if offered else
                               "Its workflow offers no move from here to this account")

    def working(self, busy, note=""):
        """A change is being made: the controls that would make another wait."""
        self._working = busy
        self._apply_mode()
        self.tell(note)

    def tell(self, text):
        """A quiet line under the issue: what was just done, or is being done."""
        self.note.setText(text)
        self.note.setVisible(bool(text))

    def _composing(self):
        self._apply_mode()

    def _apply_mode(self):
        """Reading or editing, idle or waiting on Jira: what shows, and what
        may be pressed. While editing nothing else is written to the issue -
        a change of its own would make the edit look like somebody else's.

        The place on the page is kept. A focused button that is hidden or
        disabled hands focus to the next one along, and the scroll area runs
        to it - to the attachments, from Edit at the top.
        """
        bar = self.scroll.verticalScrollBar()
        place = bar.value()
        editing = self.editing is not None
        busy = self._working
        typing = self._typing_description()
        cloud = self._cloud()
        shown = {self.save: editing, self.cancel: editing, self.summary_edit: editing,
                 self.priority_edit: editing, self.labels_edit: editing,
                 self.assignee_edit: editing,
                 self.status: not editing, self.edit: not editing, self.more: not editing,
                 self.summary: not editing, self.labels: not editing,
                 self.assignee: not editing, self.assignee_mark: not editing,
                 self.priority: not editing and bool(self.priority.text()),
                 self.description_edit: typing and not cloud,
                 self.rich_edit: typing and cloud,
                 self.description: not typing,
                 self.rich_row: bool(self._losing()) and self._may("description")}
        # What appears, before what goes: focus leaving a hidden button finds
        # its neighbour already there.
        for widget, visible in sorted(shown.items(), key=lambda pair: not pair[1]):
            widget.setVisible(visible)
        self.status.setEnabled(not busy and not self._partial
                               and bool(self.status.menu().actions()))
        self.edit.setEnabled(not busy and not self._partial)
        self.more.setEnabled(not busy)
        self.attach.setEnabled(not busy and not editing)
        self.post.setEnabled(not busy and not editing
                             and bool(self.composer.toPlainText().strip()))
        self.composer.setReadOnly(busy)
        for widget in (self.summary_edit, self.labels_edit, self.description_edit,
                       self.rich_edit):
            widget.setReadOnly(busy)
        self.priority_edit.setEnabled(not busy and self._may("priority")
                                      and self.priority_edit.count() > 1)
        self.assignee_edit.setEnabled(not busy)
        self.summary_edit.setEnabled(self._may("summary"))
        self.labels_edit.setEnabled(self._may("labels"))
        self.cancel.setEnabled(not busy)
        self.save.setEnabled(editing and not busy and bool(self.changes()))
        self.save.setText("Saving…" if editing and busy else "Save")
        self.save_key.setEnabled(editing)
        self.escape_key.setEnabled(editing)
        bar.setValue(place)

    # -- editing in place -----------------------------------------------------
    def begin_edit(self):
        """Turn the issue's fields into fields to type in. What this account
        may change is read now; until it arrives each is offered as Jira
        offered it last time, and priority waits for its list."""
        if self.issue is None or self.editing is not None or self._partial:
            return
        issue = self.issue
        self.editing = dict(issue)
        self._editable = None
        self._rewrite = False
        for widget in (self.summary_edit, self.labels_edit, self.description_edit,
                       self.priority_edit):
            widget.blockSignals(True)
        self.summary_edit.setText(issue.get("summary") or "")
        self.labels_edit.setText(" ".join(issue.get("labels") or []))
        self.description_edit.setPlainText(issue.get("description_text") or "")
        self.priority_edit.clear()
        self.priority_edit.addItem(issue.get("priority") or "No priority", "")
        for widget in (self.summary_edit, self.labels_edit, self.description_edit,
                       self.priority_edit):
            widget.blockSignals(False)
        self.assignee_edit.start(issue.get("assignee") or "")
        if self._cloud():
            self.rich_edit.set_document(issue.get("description_doc"))
        self._say_what_is_lost()
        self.edit_problem.setVisible(False)
        self.tell("")
        # Focus first, where the person is looking: the Edit button about to
        # be hidden must not hand it to something further down.
        self.summary_edit.setEnabled(True)
        self.summary_edit.setVisible(True)
        self.summary_edit.setFocus()
        self.summary_edit.end(False)
        self._apply_mode()
        self._fit_description()
        self.edit_started.emit()

    def set_meta(self, meta):
        """What the issue's screen lets this account change. A field it leaves
        out stays as it is and cannot be typed in."""
        if self.editing is None:
            return
        self._editable = set(meta.get("editable") or [])
        current = self.editing.get("priority") or ""
        self.priority_edit.blockSignals(True)
        self.priority_edit.clear()
        for one in meta.get("priorities") or []:
            self.priority_edit.addItem(one["name"], one["id"])
        index = self.priority_edit.findText(current)
        if index < 0:
            self.priority_edit.insertItem(0, current or "No priority", "")
            index = 0
        self.priority_edit.setCurrentIndex(index)
        self.priority_edit.blockSignals(False)
        self._apply_mode()

    def _may(self, field):
        """Whether a field may be changed - until Jira says, everything but
        priority, whose choices it has not sent yet."""
        if self.editing is None:
            return False
        return field != "priority" if self._editable is None else field in self._editable

    def _cloud(self):
        """Whether the description is edited with its formatting: Cloud's is
        a document, Server's is text."""
        return self.editing is not None and self.editing.get("description_doc") is not None

    def _losing(self):
        """What editing the description would lose, for a person to read."""
        if self.editing is None:
            return []
        if self._cloud():
            return list(self.editing.get("description_lost") or [])
        return [] if self.editing.get("description_plain", True) else ["formatting"]

    def _typing_description(self):
        return self._may("description") and (not self._losing() or self._rewrite)

    def changes(self):
        """Only what differs from the issue as it was when Edit was pressed."""
        if self.editing is None:
            return {}
        before, fields = self.editing, {}
        summary = self.summary_edit.text().strip()
        if self._may("summary") and summary != (before.get("summary") or ""):
            fields["summary"] = summary
        if self._typing_description() and self._cloud():
            document = self.rich_edit.changed_document()
            if document is not None:
                fields["description"] = document
        elif self._typing_description():
            text = self.description_edit.toPlainText()
            if text.strip() != (before.get("description_text") or "").strip():
                fields["description"] = text
        labels = split_labels(self.labels_edit.text())
        if self._may("labels") and labels != list(before.get("labels") or []):
            fields["labels"] = labels
        if (self._may("priority") and self.priority_edit.currentData()
                and self.priority_edit.currentText() != (before.get("priority") or "")):
            fields["priority"] = self.priority_edit.currentData()
        assignee = self.assignee_edit.value()
        if assignee is not None:
            fields["assignee"] = assignee
        return fields

    def has_changes(self):
        return bool(self.changes())

    def _edited(self, *_args):
        if self.editing is not None:
            self.edit_problem.setVisible(False)
            self.save.setEnabled(not self._working and bool(self.changes()))

    def _say_what_is_lost(self):
        lost = self._losing()
        if self._cloud():
            things = _listed(lost)
            if self._rewrite:
                self.rich_note.setText("Saving replaces the description with what is above, "
                                       "without its %s." % things)
                self.rewrite.setText("Keep it as it is")
            else:
                self.rich_note.setText("This description has %s that this editor cannot "
                                       "keep. It stays as it is unless you edit it without "
                                       "them." % things)
                self.rewrite.setText("Edit without them")
        elif self._rewrite:
            self.rich_note.setText("Saving replaces the formatted description with this "
                                   "text. Lists, tables and pictures in it are lost.")
            self.rewrite.setText("Keep the formatting")
        else:
            self.rich_note.setText(
                "This description has formatting - lists, tables or pictures - that plain "
                "text cannot hold. It stays as it is unless you rewrite it as plain text.")
            self.rewrite.setText("Rewrite as plain text")

    def _toggle_rewrite(self):
        self._rewrite = not self._rewrite
        if not self._rewrite:
            # Back to the description as it is: what was typed goes.
            self.description_edit.setPlainText(self.editing.get("description_text") or "")
            if self._cloud():
                self.rich_edit.set_document(self.editing.get("description_doc"))
        self._say_what_is_lost()
        self._apply_mode()
        self._fit_description()
        if self._rewrite:
            (self.rich_edit if self._cloud() else self.description_edit).setFocus()

    def _fit_description(self, *_args):
        """As tall as its text, up to a screenful; past that it scrolls."""
        lines = max(4, self.description_edit.document().documentLayout().documentSize()
                    .height())
        spacing = self.description_edit.fontMetrics().lineSpacing()
        wanted = int(lines * spacing + 2 * self.description_edit.frameWidth()
                     + self.description_edit.document().documentMargin() * 2 + 4)
        self.description_edit.setFixedHeight(min(DESCRIPTION_HEIGHT, wanted))

    def _save(self):
        if self.editing is None or self._working:
            return
        fields = self.changes()
        if not fields:
            self.put_down()
            return
        self.save_requested.emit({"key": self.editing.get("key"),
                                  "updated": self.editing.get("updated") or "",
                                  "fields": fields})

    def put_down(self):
        """Stop editing; what was typed is dropped and nothing is sent."""
        if self.editing is None or self._working:
            return
        self._stop_editing()
        if self.issue is not None:
            self.show_issue(self.issue)

    def may_put_down(self):
        """Stop editing if nothing was changed, or the person agrees to drop
        what was. False: they want to keep editing."""
        if self.editing is None:
            return True
        if self._working:
            return False
        if self.has_changes() and not widgets.confirm(
                self, "Discard changes",
                "Discard your changes to %s?" % self.editing.get("key", "this issue"),
                "Nothing was sent to Jira.", agree="Discard", refuse="Keep editing",
                kind="warn"):
            return False
        self.put_down()
        return True

    def _escape(self):
        if self.editing is not None:
            self.may_put_down()

    def _stop_editing(self):
        focused = QApplication.focusWidget()
        inside = focused is not None and self.isAncestorOf(focused)
        self.editing = None
        self._editable = None
        self._rewrite = False
        self.edit_problem.setVisible(False)
        if inside:
            # Back to Edit, which is where the next edit starts.
            self.edit.setVisible(True)
            self.edit.setFocus()
        self._apply_mode()

    # -- people to mention ------------------------------------------------------
    def set_cloud(self, cloud):
        """Cloud takes a comment with its formatting; Server takes text."""
        self.composer.set_plain(not cloud)

    def _want_people(self, query):
        """An editor wants people. The first read for an issue is shared: the
        other editor gets the answer too, and does not ask again."""
        if not query and self._people:
            for editor in (self.composer, self.rich_edit):
                editor.set_people(self._people)
            return
        self.people_wanted.emit(query)

    def take_people(self, key, people, query=""):
        """People who can see ``key``, read from Jira: for both editors."""
        if key != self._people_of:
            return
        known = {one["id"] for one in self._people}
        self._people += [one for one in people or [] if one.get("id") not in known]
        for editor in (self.composer, self.rich_edit):
            editor.set_people(self._people, query)

    def people_failed(self, key):
        if key == self._people_of:
            for editor in (self.composer, self.rich_edit):
                editor.people_failed()

    def add_earlier(self, page):
        """Earlier comments, read on request, under the ones shown."""
        if self.issue is None:
            return
        held = self.issue.setdefault("comments", {"items": [], "total": 0})
        held["items"] = list(held.get("items") or []) + list(page.get("items") or [])
        held["total"] = page.get("total", held.get("total", 0))
        self._fill_comments(held, False)

    def _fill_files(self, files, partial):
        _empty_layout(self.files)
        self.files_title.setText(("ATTACHMENTS  ·  %d" % len(files)) if files else "ATTACHMENTS")
        if not files:
            if not partial:
                self.files.addWidget(_label("No attachments.", "muted"))
            return
        for one in files:
            row = QHBoxLayout()
            row.setSpacing(8)
            mark = QLabel()
            mark.setPixmap(icons.pixmap("file", 16, theme.NEUTRAL[600]))
            row.addWidget(mark)
            name = widgets.elided_mono(one.get("filename") or "file", Qt.ElideMiddle)
            name.setToolTip("%s · %s" % (one.get("filename") or "", _size(one.get("size"))))
            row.addWidget(name, 1)
            if one.get("id"):
                save = QPushButton("Save…")
                save.setProperty("variant", "cell")
                save.setToolTip("Download it to this computer")
                save.clicked.connect(lambda _c=False, chosen=dict(one):
                                     self.download_requested.emit(chosen))
                row.addWidget(save)
            holder = QWidget()
            holder.setLayout(row)
            self.files.addWidget(holder)

    def _fill_comments(self, comments, partial):
        _empty_layout(self.comments)
        items = list(comments.get("items") or [])
        total = int(comments.get("total") or len(items))
        self.comments_title.setText(("COMMENTS  ·  %d" % total) if total else "COMMENTS")
        hidden = max(0, total - len(items))
        self.earlier.setVisible(bool(hidden))
        self.earlier.setText("Show earlier comments (%d)" % hidden)
        if not items:
            if not partial:
                self.comments.addWidget(_label("No comments yet.", "muted"))
            return
        # Newest first from the core; read top to bottom as a conversation.
        for one in reversed(items):
            box = QVBoxLayout()
            box.setSpacing(4)
            head = QHBoxLayout()
            head.setSpacing(6)
            head.addWidget(Initials(one.get("author") or ""))
            head.addWidget(_label(one.get("author") or "Someone"))
            when = _label(ago(one.get("created")), "muted")
            when.setToolTip(str(one.get("created") or ""))
            head.addWidget(when)
            head.addStretch(1)
            box.addLayout(head)
            text = _Rich()
            text.setText(one.get("html") or "")
            box.addWidget(text)
            holder = QWidget()
            holder.setLayout(box)
            self.comments.addWidget(holder)

    # -- the more menu --------------------------------------------------------
    def _open_in_jira(self):
        if self.issue and self.issue.get("url"):
            _open_web(self.issue["url"])

    def _copy(self, field):
        if self.issue and self.issue.get(field):
            QApplication.clipboard().setText(str(self.issue[field]))


def _issue_hit(row, connection_id, connection_name):
    """An issue as Ctrl+K shows it - a loaded one and one Jira found alike."""
    tone = "accent" if row.get("category") == "indeterminate" else "neutral"
    return search.hit(
        "%s  %s" % (row.get("key", ""), row.get("summary", "")),
        "Jira issue · %s" % (connection_name or connection_id),
        row.get("status", ""), tone,
        keys=" ".join(part for part in (row.get("type"), row.get("assignee"),
                                        row.get("priority")) if part),
        target=("issue", connection_id, row.get("key")))


def _listed(things):
    """``["tables", "pictures"]`` as "tables and pictures"."""
    things = [str(one) for one in things if one]
    if len(things) < 2:
        return things[0] if things else "things"
    return "%s and %s" % (", ".join(things[:-1]), things[-1])


def _empty_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.hide()
            widget.deleteLater()


def _size(value):
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return ("%d %s" if unit == "B" else "%.1f %s") % (size, unit)
        size /= 1024.0
    return ""


# -- the Issues tab -----------------------------------------------------------
class IssuesTab(QWidget):
    """Search, filters, the list and the opened issue. Asks nothing on its own:
    every request goes up to :class:`JiraView` as a signal."""

    search_requested = Signal()
    more_requested = Signal()
    refresh_requested = Signal()
    issue_opened = Signal(dict)
    connections_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 14, 0, 0)
        outer.setSpacing(10)

        line = QHBoxLayout()
        line.setSpacing(8)
        self.search = QLineEdit()
        self.search.setObjectName("jiraSearch")
        self.search.setClearButtonEnabled(True)
        self.search.returnPressed.connect(self.search_requested)
        self.search.textChanged.connect(self._text_changed)
        self._searched_text = ""
        line.addWidget(self.search, 1)
        self.refresh = QPushButton("")
        icons.button(self.refresh, "refresh", "")
        self.refresh.setObjectName("jiraRefresh")
        self.refresh.setToolTip("Refresh what is shown")
        self.refresh.clicked.connect(self.refresh_requested)
        line.addWidget(self.refresh)
        self.create = QPushButton("Create issue")
        self.create.setProperty("variant", "primary")
        icons.button(self.create, "plus", "Create issue", color=theme.BG)
        line.addWidget(self.create)
        outer.addLayout(line)

        filters = QHBoxLayout()
        filters.setSpacing(8)
        self.projects = widgets.capped(LazyCombo("All projects"), 220)
        self.projects.setAccessibleName("Project")
        self.statuses = widgets.capped(LazyCombo("All statuses"), 200)
        self.statuses.setAccessibleName("Status")
        self.mine = QPushButton("Assigned to me")
        self.mine.setCheckable(True)
        self.mine.setProperty("variant", "toggle")
        self.jql = QPushButton("JQL")
        self.jql.setCheckable(True)
        self.jql.setProperty("variant", "toggle")
        self.jql.setToolTip("Type a JQL query into the search box instead")
        for widget in (self.projects, self.statuses, self.mine, self.jql):
            filters.addWidget(widget)
        filters.addStretch(1)
        outer.addLayout(filters)
        self.projects.activated.connect(lambda _index: self.search_requested.emit())
        self.statuses.activated.connect(lambda _index: self.search_requested.emit())
        self.mine.toggled.connect(lambda _on: self.search_requested.emit())
        self.jql.toggled.connect(self._jql_toggled)

        self.problem = QWidget()
        problem_row = QHBoxLayout(self.problem)
        problem_row.setContentsMargins(0, 0, 0, 0)
        self.problem_text = _label("", "error", wrap=True)
        problem_row.addWidget(self.problem_text, 1)
        self.problem_action = QPushButton("Open Connections")
        self.problem_action.setProperty("variant", "ghost")
        self.problem_action.clicked.connect(self.connections_requested)
        problem_row.addWidget(self.problem_action)
        self.problem.setVisible(False)
        outer.addWidget(self.problem)

        self.splitter = widgets.FoldingSplitter(Qt.Horizontal)
        self.splitter.setObjectName("jiraSplitter")
        catalog = QWidget()
        column = QVBoxLayout(catalog)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        head = QHBoxLayout()
        self.count = widgets.kicker("Issues")
        head.addWidget(self.count, 1)
        self.order = QToolButton()
        self.order.setObjectName("jiraOrder")
        self.order.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.order.setLayoutDirection(Qt.RightToLeft)
        self.order.setToolTip("Newest first; click for oldest first")
        self.order.clicked.connect(self._flip_order)
        self._descending = True
        head.addWidget(self.order)
        column.addLayout(head)
        self.list = QListWidget()
        self.list.setObjectName("jiraIssues")
        self.list.setAccessibleName("Issues")
        self.list.setItemDelegate(IssueDelegate(self.list))
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setMouseTracking(True)
        self.list.setFrameShape(QFrame.NoFrame)
        self.list.setMinimumWidth(280)
        self.list.currentItemChanged.connect(self._picked)
        self.note = widgets.empty_note(self.list, "")
        self.list_skeleton = widgets.Skeleton(self.list, "rows")
        holder = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        holder.layout().addWidget(self.list)
        column.addWidget(holder, 1)
        self.load_more = QPushButton("Load more")
        self.load_more.setProperty("variant", "ghost")
        self.load_more.clicked.connect(self.more_requested)
        self.load_more.setVisible(False)
        column.addWidget(self.load_more, 0, Qt.AlignHCenter)
        self.splitter.addWidget(catalog)

        self.blank = QStackedWidget()
        self.detail = IssueDetail()
        self.blank.addWidget(self.detail)
        self.start = QWidget()
        start = QVBoxLayout(self.start)
        start.setContentsMargins(0, 0, 0, 0)
        self.start_zone, self.start_title, self.start_detail = zone()
        start.addWidget(self.start_zone)
        self.start_button = QPushButton("")
        self.start_button.setProperty("variant", "primary")
        self._start_action = None
        self.start_button.clicked.connect(
            lambda: self._start_action() if self._start_action else None)
        start.addWidget(self.start_button, 0, Qt.AlignHCenter)
        start.addStretch(1)
        self.blank.addWidget(self.start)
        self.splitter.addWidget(self.blank)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([420, 560])
        # Both panes scroll, so neither has to be tall: left to their contents,
        # an empty zone and its button held the whole window taller than a
        # laptop screen once the panes stood one above the other.
        for pane in (catalog, self.blank):
            pane.setMinimumHeight(90)
        # The list folds away when the issue is dragged over it - the splitter
        # leaves its bold line, and a click on it brings the list back. The
        # issue does not: it is what the list is for.
        self.splitter.setCollapsible(0, True)
        self.splitter.setCollapsible(1, False)
        outer.addWidget(self.splitter, 1)

        self.footer = _label("", "muted")
        self.footer.setAlignment(Qt.AlignRight)
        outer.addWidget(self.footer)
        self._set_order_mark()
        self._jql_toggled(False, search=False)

    # -- the controls ---------------------------------------------------------
    def filters(self):
        """What the list is asked for, as the core's ``search`` request."""
        if self.jql.isChecked():
            return {"jql": self.search.text().strip(), "order": self.order_text()}
        return {"text": self.search.text().strip(), "project": self.projects.value(),
                "status": self.statuses.value(), "mine": self.mine.isChecked(),
                "order": self.order_text()}

    def filtered(self):
        request = self.filters()
        return any(request.get(key) for key in ("text", "project", "status", "mine", "jql"))

    def order_text(self):
        return "updated DESC" if self._descending else "updated ASC"

    def set_filters(self, state):
        for widget in (self.mine, self.jql):
            widget.blockSignals(True)
        self.projects.set_value(state.get("project") or "")
        self.statuses.set_value(state.get("status") or "")
        self.mine.setChecked(bool(state.get("mine")))
        self.jql.setChecked(bool(state.get("jql_mode")))
        self._descending = state.get("order", "updated DESC") != "updated ASC"
        for widget in (self.mine, self.jql):
            widget.blockSignals(False)
        self._jql_toggled(self.jql.isChecked(), search=False)
        self._set_order_mark()

    def state(self):
        return {"project": self.projects.value(), "status": self.statuses.value(),
                "mine": self.mine.isChecked(), "jql_mode": self.jql.isChecked(),
                "order": self.order_text()}

    def clear_filters(self):
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self._searched_text = ""
        self.set_filters({"order": self.order_text()})
        self.search_requested.emit()

    def searched(self):
        """What the search box held when the list was last asked for."""
        self._searched_text = self.search.text().strip()

    def _text_changed(self, text):
        # Cleared with the box's own button: that is asking for the list
        # without the words. Typing alone asks nothing - Enter does.
        if not text.strip() and self._searched_text:
            self.search_requested.emit()

    def _jql_toggled(self, on, search=True):
        self.search.setPlaceholderText(
            'JQL, e.g. project = QA AND status = "In Progress"' if on
            else "Search issues or enter a key…")
        self.search.setProperty("mono", "true" if on else "")
        self.search.style().unpolish(self.search)
        self.search.style().polish(self.search)
        for widget in (self.projects, self.statuses, self.mine):
            widget.setEnabled(not on)
        if search and (not on or self.search.text().strip()):
            self.search_requested.emit()

    def _flip_order(self):
        self._descending = not self._descending
        self._set_order_mark()
        self.search_requested.emit()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.StyleChange, QEvent.PaletteChange):
            self.paint_icons()

    def paint_icons(self):
        """Marks are painted in an ink; a change of theme needs them again."""
        icons.button(self.refresh, "refresh", "")
        icons.button(self.create, "plus", "Create issue", color=theme.BG)
        self._set_order_mark()

    def _set_order_mark(self):
        self.order.setText("UPDATED")
        self.order.setIcon(icons.icon("sort_down" if self._descending else "sort_up",
                                      theme.NEUTRAL[600]))
        self.order.setToolTip("Newest first; click for oldest first" if self._descending
                              else "Oldest first; click for newest first")

    # -- the list -------------------------------------------------------------
    def show_rows(self, rows, more, keep_key=""):
        self.list_skeleton.stop()
        self.list.blockSignals(True)
        self.list.clear()
        for row in rows:
            item = QListWidgetItem()
            item.setData(Qt.UserRole, row)
            item.setData(Qt.AccessibleTextRole, "%s %s" % (row.get("key", ""),
                                                           row.get("summary", "")))
            self.list.addItem(item)
        self.list.blockSignals(False)
        self.load_more.setVisible(bool(more))
        self.count.setText(("%d%s ISSUES" % (len(rows), "+" if more else ""))
                           if rows else "ISSUES")
        if keep_key:
            self.select_key(keep_key, emit=False)
        self.note.say("No issues match these filters. Clear them to see more."
                      if self.filtered() else
                      "No issues were updated in the last 90 days.")

    def select_key(self, key, emit=True):
        """Pick the row of ``key``; False if it is not in the list. With
        ``emit`` False nothing is opened: the row only shows as picked."""
        found = next((index for index in range(self.list.count())
                      if (self.list.item(index).data(Qt.UserRole) or {}).get("key") == key),
                     -1)
        if found < 0 and emit:
            return False
        self.list.blockSignals(not emit)
        self.list.setCurrentRow(found)
        self.list.blockSignals(False)
        return found >= 0

    def update_row(self, issue):
        """An opened issue's newer fields, onto its row - without asking again."""
        for index in range(self.list.count()):
            item = self.list.item(index)
            row = item.data(Qt.UserRole) or {}
            if row.get("key") == issue.get("key"):
                fresh = dict(row)
                fresh.update({key: issue.get(key) for key in
                              ("summary", "type", "priority", "status", "category",
                               "assignee", "updated") if key in issue})
                item.setData(Qt.UserRole, fresh)
                return

    def rows(self):
        return [self.list.item(index).data(Qt.UserRole) for index in range(self.list.count())]

    def _picked(self, current, _previous):
        if current is not None:
            self.issue_opened.emit(dict(current.data(Qt.UserRole) or {}))

    # -- states ---------------------------------------------------------------
    def say(self, text, action=""):
        self.problem_text.setText(text)
        self.problem_action.setText(action or "Open Connections")
        self.problem_action.setVisible(bool(action))
        self.problem.setVisible(bool(text))

    def offer(self, title, detail, button, callback, loadable=False):
        """A whole-tab state with one thing to do: no connection, no token, not
        loaded. Shown instead of the list and the detail. ``loadable`` keeps the
        search and filters usable - a connection that only has not been read yet
        can be asked with them."""
        self.start_title.setText(title)
        self.start_detail.setText(detail)
        self.start_button.setText(button)
        self._start_action = callback
        self.blank.setCurrentWidget(self.start)
        self.splitter.widget(0).setVisible(False)
        self.load_more.setVisible(False)
        # Nothing to search or filter yet: the controls say so rather than
        # take a click that does nothing.
        self._controls(loadable)

    def ready(self):
        self.blank.setCurrentWidget(self.detail)
        self.splitter.widget(0).setVisible(True)
        self._controls(True)

    def _controls(self, on):
        for widget in (self.search, self.refresh, self.jql, self.create):
            widget.setEnabled(on)
        for widget in (self.projects, self.statuses, self.mine):
            widget.setEnabled(on and not self.jql.isChecked())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        orientation = Qt.Vertical if self.width() < 760 else Qt.Horizontal
        if self.splitter.orientation() != orientation:
            # Side by side is how a person divided it - a folded list included -
            # and stacking for a narrow moment must not lose that.
            if self.splitter.orientation() == Qt.Horizontal and self.isVisible():
                self._across = self.splitter.sizes()
            self.splitter.setOrientation(orientation)
            self.splitter.setSizes([260, 420] if orientation == Qt.Vertical
                                   else getattr(self, "_across", None) or [420, 560])


# -- the view -----------------------------------------------------------------
class _Cache(object):
    """What one connection has loaded this session. Never written anywhere."""

    def __init__(self):
        self.rows, self.cursor, self.loaded_at, self.request = [], None, None, None
        self.details = {}
        self.opened = ""


class JiraView(QWidget):
    """Jira under Integrations: the header, its connection, and four tabs."""

    back_requested = Signal()
    connections_changed = Signal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.core = None
        self.connections = []
        self.cache = {}
        self.health = {}
        self._asked = {}
        self._listed = False
        self._listing = False
        self._started = False
        self._closing = False
        self._pending = {}  # cancellation event -> request kind
        self._editing_on = ""  # the connection of the issue being edited
        # Issues Ctrl+K found in Jira, by (connection, key): what their row in
        # the list is, when one is opened before the list has it.
        self._found = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 18, 24, 14)
        outer.setSpacing(4)
        crumb = QHBoxLayout()
        self.crumb = QPushButton("Integrations  /")
        self.crumb.setObjectName("jiraCrumb")
        self.crumb.setProperty("variant", "ghost")
        self.crumb.setCursor(Qt.PointingHandCursor)
        self.crumb.clicked.connect(self.back_requested)
        crumb.addWidget(self.crumb)
        crumb.addStretch(1)
        self.window_actions = crumb
        outer.addLayout(crumb)
        header = QHBoxLayout()
        header.setSpacing(10)
        header.addWidget(widgets.heading("Jira"))
        self.tag = widgets.Tag("", "neutral")
        header.addWidget(self.tag, 0, Qt.AlignVCenter)
        header.addStretch(1)
        header.addWidget(_label("Connection", "muted"))
        self.picker = widgets.capped(QComboBox(), 240)
        self.picker.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.picker.setMinimumContentsLength(12)
        # Never a blank box: with nothing to choose it says so, greyed.
        self.picker.setPlaceholderText("No connection yet")
        self.picker.setAccessibleName("Connection")
        self.picker.activated.connect(self._connection_picked)
        header.addWidget(self.picker)
        outer.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("jiraTabs")
        self.issues = IssuesTab()
        self.connections_tab = ConnectionsTab()
        self.activity = ActivityTab()
        self.settings_tab = SettingsTab()
        self.tabs.addTab(self.issues, "Issues")
        self.tabs.addTab(self.connections_tab, "Connections")
        self.tabs.addTab(self.activity, "Activity")
        self.tabs.addTab(self.settings_tab, "Settings")
        self.tabs.currentChanged.connect(self._tab_changed)
        outer.addWidget(self.tabs, 1)
        # Where the list and the issue were divided - folded list included. A
        # page laid out narrow for a moment while it is built must come back
        # to that, not to the default.
        self.issues.splitter.remember_in(settings, "integrations/jira")
        if settings.splitter("integrations/jira"):
            self.issues._across = self.issues.splitter.sizes()

        self.issues.search_requested.connect(self.search)
        self.issues.more_requested.connect(self.load_more)
        self.issues.refresh_requested.connect(self.refresh)
        self.issues.issue_opened.connect(self.open_issue)
        self.issues.connections_requested.connect(
            lambda: self.tabs.setCurrentWidget(self.connections_tab))
        self.issues.projects.wanted.connect(lambda: self._choices("projects"))
        self.issues.statuses.wanted.connect(lambda: self._choices("statuses"))
        self.issues.detail.earlier_requested.connect(self.earlier_comments)
        self.issues.create.clicked.connect(self.create_issue)
        detail = self.issues.detail
        detail.transition_requested.connect(self.move_issue)
        detail.edit_started.connect(self.edit_issue)
        detail.save_requested.connect(self.save_edit)
        detail.assignee_edit.wanted.connect(self.find_people)
        detail.assign_requested.connect(self.assign_issue)
        detail.reload_requested.connect(lambda: self.open_issue(detail.issue or {}))
        detail.attach_requested.connect(self.attach_file)
        detail.download_requested.connect(self.save_file)
        detail.comment_requested.connect(self.post_comment)
        detail.people_wanted.connect(self.find_mentionable)
        self.activity.wanted.connect(self.load_activity)
        self._activity_stale = True
        self.connections_tab.save_requested.connect(self.save_connection)
        self.connections_tab.test_requested.connect(self.test_connection)
        self.connections_tab.remove_requested.connect(self.remove_connection)
        self.connections_tab.sign_in_requested.connect(self.sign_in)
        self.connections_tab.app_requested.connect(self.open_app)
        self.settings_tab.save_requested.connect(self.save_connection)

        self.ticker = QTimer(self)
        self.ticker.setInterval(30000)
        self.ticker.timeout.connect(self._show_footer)
        self.auto = QTimer(self)
        self.auto.timeout.connect(self._auto_refresh)

    # -- the outside ----------------------------------------------------------
    def set_core(self, core):
        """A core for the project in use. Another core is another project, or
        other paths: everything loaded so far belonged to the old one."""
        if core is self.core:
            return
        self._cancel_requests()
        self.core = core
        self.connections, self.cache, self.health = [], {}, {}
        self._found = {}
        self._asked = {}
        self._listed = False
        self._listing = False
        self._started = False
        self._activity_stale = True
        self.auto.stop()
        self.issues.projects.reset()
        self.issues.statuses.reset()
        self.issues.list_skeleton.stop()
        self.issues.load_more.setEnabled(True)
        self.issues.show_rows([], False)
        self.issues.detail.clear()
        self.issues.detail.composer.clear()
        self.issues.detail.working(False)
        self.activity.set_rows([])
        self.connections_tab.message.clear()
        self.issues.search.clear()
        self.issues._searched_text = ""
        self.picker.blockSignals(True)
        self.picker.clear()
        self.picker.blockSignals(False)
        self.connections_tab.set_connections([])
        self.settings_tab.set_connection(None)
        self._show_health()
        state = self.settings.integrations_state()
        self.issues.set_filters(state.get("filters") or {})
        if self.isVisible():
            self.list_connections()
            if self.tabs.currentWidget() is self.activity:
                self.load_activity()

    def busy_reason(self, include_dialogs=True):
        if any(kind in WRITES for kind in self._pending.values()):
            return "A Jira change is being saved. Wait for it to finish."
        if include_dialogs and any(dialog.isVisible() for dialog in self.findChildren(QDialog)):
            return "Finish or cancel the open Jira dialog first."
        return ""

    def _cancel_requests(self):
        """Stop the reads in flight. A write is never stopped: killed half way,
        nobody could say whether Jira took it. It runs to its end, and its
        answer is dropped when the context it belonged to is gone."""
        for cancel, kind in list(self._pending.items()):
            if kind not in WRITES:
                cancel.set()
                del self._pending[cancel]
        self._asked.clear()

    def shutdown(self):
        self._closing = True
        self.ticker.stop()
        self.auto.stop()
        self._cancel_requests()
        for dialog in self.findChildren(QDialog):
            dialog.reject()

    def current(self):
        connection_id = self.picker.currentData()
        return next((one for one in self.connections if one.get("id") == connection_id), None)

    def showEvent(self, event):
        super().showEvent(event)
        if self._closing:
            return
        self.ticker.start()
        if not self._listed:
            self.list_connections()
        elif not self._started:
            self._first_look()
        self._arm_auto()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.ticker.stop()
        self.auto.stop()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.StyleChange, QEvent.PaletteChange):
            self._show_health()
            self.issues._set_order_mark()
            self.issues.list.viewport().update()

    # -- asking the core ------------------------------------------------------
    def _call(self, kind, work, done, quiet=False, cancel=None):
        """``work(core, cancel)`` on a thread; the latest answer reaches ``done``.

        A core that could not answer at all is said on the Issues tab; a failure
        the core reports (``ok`` false) is the caller's to show.
        """
        if self._closing or self.core is None:
            return
        self._asked[kind] = self._asked.get(kind, 0) + 1
        mine = self._asked[kind]
        core = self.core
        cancel = cancel if cancel is not None else threading.Event()
        self._pending[cancel] = kind
        # A write's process is not told about this cancel: see _cancel_requests.
        handed = None if kind in WRITES else cancel

        def answer(result, error):
            self._pending.pop(cancel, None)
            if self._closing or self._asked.get(kind) != mine or core is not self.core:
                return
            if error is not None:
                if not isinstance(error, core_mod.CoreError):
                    raise error
                if not quiet:
                    self.issues.say(str(error))
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
        """The project's connections - a file the core reads; Jira is not asked."""
        if self.core is None or self._listing:
            return
        self._listing = True

        def done(payload):
            self._listing = False
            self._listed = True
            if payload.get("ok"):
                self._set_connections(payload.get("connections") or [])
            else:
                self.issues.say(payload.get("error") or "The connections could not be read.")
            if then is not None:
                then()
            self._first_look()

        self._call("list", lambda core, cancel: core.integrations("list", cancel=cancel), done)

    def _set_connections(self, connections, pick=""):
        self.connections = list(connections)
        wanted = pick or self.picker.currentData() or \
            self.settings.integrations_state().get("connection", "")
        self.picker.blockSignals(True)
        self.picker.clear()
        for one in self.connections:
            self.picker.addItem(one.get("name") or one.get("id"), one.get("id"))
        index = self.picker.findData(wanted)
        self.picker.setCurrentIndex(index if index >= 0 else (0 if self.connections else -1))
        self.picker.setEnabled(bool(self.connections))
        self.picker.blockSignals(False)
        self.connections_tab.set_connections(self.connections, self.health)
        self.settings_tab.set_connection(self.current())
        self._show_health()
        self._show_issues()
        self.connections_changed.emit()

    def _connection_picked(self, _index):
        """Another connection picked. An issue being edited belongs to the one
        it was opened through: leaving that puts the edit down, or - if the
        person would rather keep it - puts the picker back."""
        detail = self.issues.detail
        if (detail.editing is not None and self.picker.currentData() != self._editing_on
                and not detail.may_put_down()):
            self.picker.setCurrentIndex(max(0, self.picker.findData(self._editing_on)))
            return False
        self._remember()
        self.settings_tab.set_connection(self.current())
        self._show_health()
        self._show_issues()
        self._arm_auto()
        return True

    def _remember(self):
        state = dict(self.settings.integrations_state())
        state.update({"connection": self.picker.currentData() or "",
                      "filters": self.issues.state()})
        self.settings.save_integrations_state(state)

    # -- the header -----------------------------------------------------------
    def _show_health(self):
        connection = self.current()
        if connection is None:
            self.tag.set("Not connected", "neutral")
            return
        if not connection.get("token_set"):
            self.tag.set(*HEALTH["no_token"][:2])
            return
        kind = self.health.get(connection["id"])
        if kind in HEALTH:
            self.tag.set(*HEALTH[kind][:2])
        elif kind:
            self.tag.set("Problem", "warn")
        elif (connection.get("last_check") or {}).get("ok"):
            self.tag.set("Connected", "accent")
        else:
            self.tag.set("Not checked", "neutral")
        check = connection.get("last_check") or {}
        self.tag.setToolTip("Signed in as %s, %s" % (check.get("account") or "?",
                                                     ago(check.get("at", "").replace(" ", "T")))
                            if check.get("ok") else "")

    def _heard(self, connection_id, payload):
        """Note what a real request said about a connection, and show it."""
        kind = "ok" if payload.get("ok") else payload.get("kind") or "error"
        if kind in ("invalid", "not_found") and not payload.get("ok"):
            kind = "ok"     # Jira answered; it was the question that was wrong
        self.health[connection_id] = kind
        self._show_health()
        self.connections_tab.set_connections(self.connections, self.health)
        return kind

    # -- the Issues tab: what is shown ----------------------------------------
    def _tab_changed(self, _index):
        self._first_look()
        if self.tabs.currentWidget() is self.activity:
            self.activity.shown()

    def _first_look(self):
        """The Issues tab seen for the first time since the application started:
        its one automatic request - the first page of the list."""
        if not self.isVisible() or self.tabs.currentWidget() is not self.issues:
            return
        connection = self.current()
        if (not self._started and connection is not None and connection.get("token_set")
                and not self._cache(connection).loaded_at):
            self._started = True
            self.search()
            return
        self._show_issues()

    def _cache(self, connection):
        return self.cache.setdefault(connection["id"], _Cache())

    def _show_issues(self):
        connection = self.current()
        self.issues.say("")
        self.issues.detail.set_cloud((connection or {}).get("flavour", "cloud") == "cloud")
        if connection is None:
            self.issues.offer(
                "No Jira connection yet",
                "A connection is a Jira site and an account, set once for this "
                "project. Cycles can then name it instead of carrying a site and a token.",
                "Add connection", self._add_from_issues)
            self._show_footer()
            return
        if not connection.get("token_set"):
            self.issues.offer(
                "%s has no token" % connection.get("name"),
                "Set its API token in Connections; nothing is asked of Jira until then.",
                "Set the token", lambda: self._edit_from_issues(connection))
            self._show_footer()
            return
        cache = self._cache(connection)
        if cache.loaded_at is None:
            self.issues.offer(
                "Issues of %s are not loaded" % connection.get("name"),
                "Nothing is asked of Jira until you do. Loading reads one page of the "
                "list with the filters above.",
                "Load issues", self.search, loadable=True)
            self._show_footer()
            return
        self.issues.ready()
        self.issues.show_rows(cache.rows, cache.cursor, keep_key=cache.opened)
        if self.issues.detail.editing is not None:
            pass  # stays until it is saved or put down
        elif cache.opened and cache.opened in cache.details:
            self.issues.detail.show_issue(cache.details[cache.opened])
        elif not cache.opened:
            self.issues.detail.clear()
        kind = self.health.get(connection["id"])
        if kind in HEALTH and HEALTH[kind][2]:
            self.issues.say(HEALTH[kind][2],
                            "Open Connections" if kind in ("signin", "no_token") else "")
        self._show_footer()

    def _show_footer(self):
        connection = self.current()
        if connection is None:
            self.issues.footer.setText("")
            return
        cache = self.cache.get(connection["id"])
        when = cache.loaded_at if cache is not None else None
        self.issues.footer.setText("%s  ·  %s" % (
            connection.get("name"),
            "Updated %s" % ago(when) if when else "Not loaded"))

    def _add_from_issues(self):
        self.tabs.setCurrentWidget(self.connections_tab)
        self.connections_tab.add()

    def _edit_from_issues(self, connection):
        self.tabs.setCurrentWidget(self.connections_tab)
        self.connections_tab.edit(connection)

    # -- the Issues tab: asking -----------------------------------------------
    def search(self):
        """The first page of the list, with the filters as they are now."""
        connection = self.current()
        if connection is None or not connection.get("token_set"):
            self._show_issues()
            return
        self._started = True
        self._remember()
        request = dict(self.issues.filters(), size=PAGE_ROWS)
        if self.issues.jql.isChecked() and not request.get("jql"):
            self.issues.say("Type a JQL query and press Enter.")
            return
        self.issues.searched()
        self.issues.ready()
        if not self._cache(connection).rows:
            self.issues.list_skeleton.start()
        self._list(connection, request, replace=True)

    def load_more(self):
        connection = self.current()
        cache = self.cache.get((connection or {}).get("id"))
        if connection is None or cache is None or not cache.cursor:
            return
        request = dict(cache.request or {}, cursor=cache.cursor, size=PAGE_ROWS)
        self.issues.load_more.setEnabled(False)
        self._list(connection, request, replace=False)

    def refresh(self):
        """The rows on screen and the open issue, asked again - nothing more."""
        connection = self.current()
        if connection is None or not connection.get("token_set"):
            self._show_issues()
            return
        cache = self._cache(connection)
        if cache.loaded_at is None or cache.request is None:
            self.search()
            return
        request = {"list": dict(cache.request, size=max(PAGE_ROWS, len(cache.rows)),
                                cursor=None)}
        if cache.opened:
            request["issue"] = {"key": cache.opened, "comments": COMMENTS}
        connection_id = connection["id"]

        def done(payload):
            kind = self._heard(connection_id, payload)
            if not payload.get("ok"):
                self.issues.say(payload.get("error") or "Jira did not answer.",
                                "Open Connections" if kind in ("signin", "no_token") else "")
                return
            result = payload.get("result") or {}
            listed = result.get("list") or {}
            cache.rows, cache.cursor = listed.get("rows") or [], listed.get("cursor")
            cache.loaded_at = datetime.datetime.now(datetime.timezone.utc)
            issue = result.get("issue")
            if issue:
                cache.details[issue["key"]] = issue
            elif cache.opened and result.get("issue_error"):
                cache.details.pop(cache.opened, None)
            if self.current() is connection:
                self._show_issues()
                if cache.opened and result.get("issue_error"):
                    self.issues.detail.unavailable(cache.opened, result["issue_error"])

        self._call("list", lambda core, cancel: core.integrations(
            "call", connection_id, "refresh", document=request, cancel=cancel), done)

    def _list(self, connection, request, replace):
        cache = self._cache(connection)
        connection_id = connection["id"]

        def done(payload):
            self.issues.load_more.setEnabled(True)
            self.issues.list_skeleton.stop()
            kind = self._heard(connection_id, payload)
            if not payload.get("ok"):
                self.issues.say(payload.get("error") or "Jira did not answer.",
                                "Open Connections" if kind in ("signin", "no_token") else "")
                if cache.loaded_at is None:
                    self.issues.show_rows([], False)
                    self.issues.note.say("Nothing loaded. Refresh asks again.")
                return
            result = payload.get("result") or {}
            rows = result.get("rows") or []
            if replace:
                cache.rows = rows
                cache.request = {key: value for key, value in request.items()
                                 if key not in ("cursor", "size")}
            else:
                known = {one.get("key") for one in cache.rows}
                cache.rows = cache.rows + [one for one in rows if one.get("key") not in known]
            cache.cursor = result.get("cursor")
            cache.loaded_at = datetime.datetime.now(datetime.timezone.utc)
            if (replace and self.issues.detail.editing is None
                    and cache.opened not in {one.get("key") for one in cache.rows}):
                cache.opened = ""
            if self.current() is connection:
                self._show_issues()

        self._call("list", lambda core, cancel: core.integrations(
            "call", connection_id, "search", document=request, cancel=cancel), done)

    def open_issue(self, row):
        """An issue opened: read it, alone, with its newest comments."""
        connection = self.current()
        key = (row or {}).get("key")
        if connection is None or not key:
            return
        detail = self.issues.detail
        editing = (detail.editing or {}).get("key")
        if editing and editing != key and not detail.may_put_down():
            # Keep editing: the row picked goes back to the one being edited.
            self.issues.select_key(editing, emit=False)
            return
        cache = self._cache(connection)
        cache.opened = key
        held = cache.details.get(key)
        if held:
            self.issues.detail.show_issue(held)
            self.issues.detail.skeleton.start()
        else:
            self.issues.detail.loading(row)
        connection_id = connection["id"]

        def done(payload):
            self._heard(connection_id, payload)
            if not payload.get("ok"):
                if payload.get("kind") == "not_found":
                    cache.details.pop(key, None)
                    if cache.opened == key:
                        self.issues.detail.unavailable(
                            key, "It was deleted or moved, or this account can no "
                                 "longer see it. Refresh updates the list.")
                    return
                if cache.opened == key:
                    self.issues.detail.say(payload.get("error") or "Jira did not answer.")
                return
            issue = (payload.get("result") or {}).get("issue") or {}
            cache.details[key] = issue
            self.issues.update_row(issue)
            for one in cache.rows:
                if one.get("key") == key:
                    one.update({field: issue.get(field) for field in
                                ("summary", "status", "category", "assignee", "priority")})
            if cache.opened == key and self.current() is connection:
                self.issues.detail.show_issue(issue)

        self._call("issue", lambda core, cancel: core.integrations(
            "call", connection_id, "issue", document={"key": key, "comments": COMMENTS},
            cancel=cancel), done)

    def earlier_comments(self):
        connection = self.current()
        detail = self.issues.detail.issue
        if connection is None or not detail:
            return
        key = detail.get("key")
        start = len((detail.get("comments") or {}).get("items") or [])
        connection_id = connection["id"]
        self.issues.detail.earlier.setEnabled(False)

        def done(payload):
            self.issues.detail.earlier.setEnabled(True)
            self._heard(connection_id, payload)
            if not payload.get("ok"):
                self.issues.detail.say(payload.get("error") or "Jira did not answer.")
                return
            if (self.issues.detail.issue or {}).get("key") == key:
                self.issues.detail.add_earlier(
                    (payload.get("result") or {}).get("comments") or {})

        self._call("comments", lambda core, cancel: core.integrations(
            "call", connection_id, "comments",
            document={"key": key, "start": start, "size": COMMENTS}, cancel=cancel), done)

    def _choices(self, which):
        """A filter's list, read the first time its box is opened."""
        connection = self.current()
        combo = self.issues.projects if which == "projects" else self.issues.statuses
        if connection is None or not connection.get("token_set"):
            combo.failed()
            return
        connection_id = connection["id"]

        def done(payload):
            self._heard(connection_id, payload)
            if not payload.get("ok"):
                combo.failed()
                self.issues.say(payload.get("error") or "Jira did not answer.")
                return
            result = payload.get("result") or {}
            if which == "projects":
                combo.set_choices([("%s  ·  %s" % (one["name"], one["key"]), one["key"])
                                   for one in result.get("projects") or []])
            else:
                combo.set_choices([(one["name"], one["name"])
                                   for one in result.get("statuses") or []])

        self._call(which, lambda core, cancel: core.integrations(
            "call", connection_id, which, cancel=cancel), done)

    # -- the Issues tab: changing ---------------------------------------------
    def has_unsaved(self):
        """Drafts and settings that leaving the project would lose."""
        return (bool(self.issues.detail.composer.toPlainText().strip())
                or self.issues.detail.has_changes()
                or self.settings_tab.save.isEnabled())

    def _write(self, operation, request, then=None, note=""):
        """One change to Jira. The answer carries the issue as it is now, so the
        list and the detail take it without asking again."""
        connection = self.current()
        detail = self.issues.detail
        if connection is None:
            return
        connection_id = connection["id"]
        detail.working(True, note)

        def answered(payload):
            detail.working(False)
            self._heard(connection_id, payload)
            if payload.get("ok"):
                result = payload.get("result") or {}
                if result.get("issue"):
                    self._took(connection, result["issue"])
                detail.tell(_done_text(operation, result))
            elif payload.get("kind") != "fields":
                detail.say(payload.get("error") or "Jira did not take the change.")
            self._activity_changed()
            if then is not None:
                then(payload)

        self._call("write", lambda core, cancel: core.integrations(
            "call", connection_id, operation, document=request, cancel=cancel),
            answered, quiet=True)

    def _took(self, connection, issue):
        """An issue as a change left it: into the cache, its row and the detail."""
        cache = self._cache(connection)
        key = issue.get("key")
        cache.details[key] = issue
        light = {field: issue.get(field) for field in
                 ("key", "summary", "type", "priority", "status", "category", "assignee",
                  "updated", "url")}
        for one in cache.rows:
            if one.get("key") == key:
                one.update(light)
                break
        else:
            cache.rows.insert(0, light)
            cache.loaded_at = cache.loaded_at or datetime.datetime.now(datetime.timezone.utc)
        cache.opened = key
        if self.current() is connection:
            self.issues.ready()
            self.issues.show_rows(cache.rows, cache.cursor, keep_key=key)
            self.issues.detail.show_issue(issue)

    def move_issue(self, chosen, fields=None):
        issue = self.issues.detail.issue or {}
        request = {"key": issue.get("key"), "to": chosen.get("id")}
        if fields:
            request["fields"] = fields

        def then(payload):
            if payload.get("kind") != "fields":
                return
            from .jiradialogs import TransitionFieldsDialog
            dialog = TransitionFieldsDialog(self, payload.get("transition") or chosen.get("name"),
                                            payload.get("needs") or [])
            if dialog.exec() == QDialog.Accepted:
                self.move_issue(chosen, dialog.fields())
            dialog.deleteLater()

        self._write("transition", request, then, note="Moving %s…" % issue.get("key", ""))

    def edit_issue(self):
        """Editing began: read what this account may change on the issue - one
        request, asked because Edit was pressed."""
        detail = self.issues.detail
        connection = self.current()
        if detail.editing is None or connection is None:
            return
        key = detail.editing.get("key")
        self._editing_on = connection["id"]

        def meta(payload):
            if (detail.editing or {}).get("key") != key:
                return
            if payload.get("ok"):
                detail.set_meta(payload.get("result") or {})
            else:
                detail.say(payload.get("error") or "Jira did not say what may be changed.")

        self._call("edit_meta", lambda core, cancel: core.integrations(
            "call", connection["id"], "edit_meta", document={"key": key},
            cancel=cancel), meta, quiet=True)

    def find_people(self, query):
        """People the issue being edited may be assigned to, matching ``query``:
        read because its list was opened, or a name typed and Enter pressed."""
        detail = self.issues.detail
        picker = detail.assignee_edit
        connection = self.current()
        if detail.editing is None or connection is None:
            picker.failed()
            return
        key = detail.editing.get("key")

        def done(payload):
            if (detail.editing or {}).get("key") != key:
                return
            if payload.get("ok"):
                picker.set_people((payload.get("result") or {}).get("people") or [])
            else:
                picker.failed()
                detail.say(payload.get("error") or "Jira did not say who it may be assigned to.")

        self._call("people", lambda core, cancel: core.integrations(
            "call", connection["id"], "assignable", document={"key": key, "query": query},
            cancel=cancel), done, quiet=True)

    def save_edit(self, request):
        """Send an edit. Taken, the issue comes back as it is now and editing
        ends; refused - changed in Jira meanwhile, say - it stays open with
        what was typed, and the reason under Save."""
        detail = self.issues.detail

        def then(payload):
            if payload.get("ok"):
                detail.put_down()

        self._write("edit", request, then, note="Saving…")

    def assign_issue(self, account):
        issue = self.issues.detail.issue or {}
        self._write("assign", {"key": issue.get("key"), "account": account},
                    note="Assigning…")

    def find_mentionable(self, query):
        """People who can see the opened issue, for a mention: read when "@"
        is first typed in it, or when a name is searched for. On a thread;
        typing goes on meanwhile."""
        detail = self.issues.detail
        connection = self.current()
        key = (detail.issue or {}).get("key")
        if connection is None or not key:
            detail.people_failed(key)
            return

        def done(payload):
            if payload.get("ok"):
                detail.take_people(key, (payload.get("result") or {}).get("people") or [],
                                   query)
            else:
                detail.people_failed(key)

        self._call("mentions", lambda core, cancel: core.integrations(
            "call", connection["id"], "mentionable", document={"key": key, "query": query},
            cancel=cancel), done, quiet=True)

    def post_comment(self, body):
        issue = self.issues.detail.issue or {}

        def then(payload):
            if payload.get("ok"):
                self.issues.detail.composer.clear()

        self._write("comment", {"key": issue.get("key"), "body": body}, then,
                    note="Posting…")

    def attach_file(self):
        issue = self.issues.detail.issue or {}
        path = widgets.pick_path(self, "Attach a file to %s" % issue.get("key", ""))
        if path:
            self._write("attach", {"key": issue.get("key"), "path": path},
                        note="Uploading %s…" % path.replace("\\", "/").rsplit("/", 1)[-1])

    def save_file(self, attachment):
        """Download one attachment where the person says - a read, asked for."""
        issue = self.issues.detail.issue or {}
        connection = self.current()
        target = widgets.pick_path(self, "Save %s" % (attachment.get("filename") or "file"),
                                   attachment.get("filename") or "", save=True)
        if not target or connection is None:
            return
        self.issues.detail.tell("Downloading %s…" % (attachment.get("filename") or "file"))

        def done(payload):
            if payload.get("ok"):
                self.issues.detail.tell("Saved to %s." % (payload.get("result") or {}).get(
                    "path", target))
            else:
                self.issues.detail.tell("")
                self.issues.detail.say(payload.get("error") or "Not saved.")

        self._call("download", lambda core, cancel: core.integrations(
            "call", connection["id"], "download",
            document={"key": issue.get("key"), "id": attachment.get("id"),
                      "url": attachment.get("url") or "", "to": target}, cancel=cancel),
            done, quiet=True)

    def create_issue(self):
        """The create window, with the projects the account can see - read now
        if the filter has not read them yet."""
        connection = self.current()
        if connection is None:
            return
        combo = self.issues.projects
        if combo.loaded():
            self._create_with([(combo.itemData(i), combo.itemText(i).split("  ·  ")[0])
                               for i in range(1, combo.count()) if combo.itemData(i)])
            return

        def done(payload):
            if not payload.get("ok"):
                self.issues.say(payload.get("error") or "The projects could not be read.")
                return
            projects = (payload.get("result") or {}).get("projects") or []
            combo.set_choices([("%s  ·  %s" % (one["name"], one["key"]), one["key"])
                               for one in projects])
            self._create_with([(one["key"], one["name"]) for one in projects])

        self._call("projects", lambda core, cancel: core.integrations(
            "call", connection["id"], "projects", cancel=cancel), done)

    def _create_with(self, projects):
        from .jiradialogs import CreateIssueDialog
        connection = self.current()
        if connection is None:
            return
        default = self.issues.projects.value() or (connection.get("settings") or {}).get(
            "default_project", "")
        dialog = CreateIssueDialog(self, projects, default)
        dialog.meta_requested.connect(lambda request, done: self._call(
            "create_meta", lambda core, cancel: core.integrations(
                "call", connection["id"], "create_meta", document=request, cancel=cancel),
            done, quiet=True))
        dialog.create_requested.connect(
            lambda request, done: self._write("create", request, done, note="Creating…"))
        dialog.exec()
        dialog.deleteLater()

    # -- the Activity tab -----------------------------------------------------
    def load_activity(self):
        """The project's log of changes - a file the core reads; Jira is not asked."""
        if self.core is None:
            return
        self._activity_stale = False
        names = {one.get("id"): one.get("name") for one in self.connections}

        def done(payload):
            if payload.get("ok"):
                self.activity.set_rows(payload.get("activity") or [], names)

        self._call("activity", lambda core, cancel: core.integrations(
            "activity", cancel=cancel), done, quiet=True)

    def _activity_changed(self):
        if self.tabs.currentWidget() is self.activity and self.isVisible():
            self.load_activity()
        else:
            self._activity_stale = True

    # -- Ctrl+K ---------------------------------------------------------------
    def search_rows(self):
        names = {one.get("id"): one.get("name") or one.get("id") for one in self.connections}
        found = []
        for one in self.connections:
            found.append(search.hit(
                one.get("name") or one.get("id"), "Jira connection · %s" % one.get("site", ""),
                "", keys=" ".join(part for part in (one.get("id"), one.get("email")) if part),
                target=("connection", one.get("id"))))
        for connection_id, cache in self.cache.items():
            for row in cache.rows:
                found.append(_issue_hit(row, connection_id,
                                        names.get(connection_id, connection_id)))
        return found

    def search_source(self):
        """What Ctrl+K offers to search in Jira: ``(title, detail)``, or None
        when no connection has a token. Offering it asks Jira nothing. The
        connections are a file the core reads; the first time Search asks,
        they are read, and the row is there from the next look."""
        if not self._listed:
            self.list_connections()
            return None
        ready = [one for one in self.connections if one.get("token_set")]
        if not ready:
            return None
        return "Jira", "Issues in %s" % ", ".join(one.get("name") or one.get("id")
                                                   for one in ready)

    def search_remote(self, query, done):
        """Search Jira for ``query`` because its row in Ctrl+K was chosen: one
        request per connection with a token, on a thread. Every project, not
        only the default one: this is looking for an issue, not a list."""
        ready = [one for one in self.connections if one.get("token_set")]
        if self.core is None or not ready:
            done([], "No Jira connection has a token.")
            return
        answers = {}

        def answered(connection, payload):
            kind = self._heard(connection["id"], payload)
            if payload.get("ok"):
                rows = (payload.get("result") or {}).get("rows") or []
                for row in rows:
                    self._found[(connection["id"], row.get("key"))] = dict(row)
                answers[connection["id"]] = ([_issue_hit(row, connection["id"],
                                                         connection.get("name"))
                                              for row in rows], "")
            else:
                answers[connection["id"]] = ([], payload.get("error") or kind or
                                             "Jira did not answer.")
            if len(answers) == len(ready):
                hits = [found for one in ready for found in answers[one["id"]][0]]
                errors = [answers[one["id"]][1] for one in ready if answers[one["id"]][1]]
                done(hits, "" if hits or not errors else errors[0])

        for connection in ready:
            self._call("found:%s" % connection["id"],
                       lambda core, cancel, one=connection: core.integrations(
                           "call", one["id"], "search",
                           document={"text": query, "size": FOUND_ROWS, "everywhere": True},
                           cancel=cancel),
                       lambda payload, one=connection: answered(one, payload), quiet=True)

    def show_found(self, target):
        if not target:
            return False
        if target[0] == "connection":
            if not any(one.get("id") == target[1] for one in self.connections):
                return False
            self.tabs.setCurrentWidget(self.connections_tab)
            self.connections_tab.select(target[1])
            return True
        if target[0] == "issue" and len(target) == 3:
            _kind, connection_id, key = target
            cache = self.cache.get(connection_id)
            listed = cache is not None and any(row.get("key") == key for row in cache.rows)
            found = self._found.get((connection_id, key))
            if not listed and found is None:
                return False
            index = self.picker.findData(connection_id)
            if index < 0:
                return False
            if self.picker.currentIndex() != index:
                self.picker.setCurrentIndex(index)
                if not self._connection_picked(index):
                    return True
            if not listed:
                # Found in Jira by Ctrl+K: its row goes at the top of the list,
                # as a created issue's does, and opening it reads it.
                cache = self._cache(self.current())
                cache.rows.insert(0, dict(found))
                cache.loaded_at = cache.loaded_at or datetime.datetime.now(
                    datetime.timezone.utc)
            self.tabs.setCurrentWidget(self.issues)
            self._show_issues()
            # Opening it is what a click on its row does: it reads that issue.
            return self.issues.select_key(key)
        return False

    # -- refreshing on a timer ------------------------------------------------
    def _arm_auto(self):
        connection = self.current()
        minutes = int(((connection or {}).get("settings") or {}).get("refresh_minutes") or 0)
        if minutes > 0 and self.isVisible() and not self._closing:
            self.auto.start(minutes * 60000)
        else:
            self.auto.stop()

    def _auto_refresh(self):
        connection = self.current()
        if (self.isVisible() and connection is not None
                and self._cache(connection).loaded_at is not None):
            self.refresh()

    # -- connections ----------------------------------------------------------
    def save_connection(self, document, done=None):
        """Add or change a connection; ``done(payload)`` hears the answer."""
        def answered(payload):
            if payload.get("ok"):
                saved = payload.get("connection") or {}
                others = [one for one in self.connections if one.get("id") != saved.get("id")]
                place = next((index for index, one in enumerate(self.connections)
                              if one.get("id") == saved.get("id")), len(others))
                others.insert(place, saved)
                self.health.pop(saved.get("id"), None)
                if _moved(self.connections, saved):
                    self.cache.pop(saved.get("id"), None)
                self._set_connections(others, pick=saved.get("id"))
                self._arm_auto()
            if done is not None:
                done(payload)

        self._call("save", lambda core, cancel: core.integrations(
            "save", document=document, cancel=cancel),
                   answered, quiet=True)

    def test_connection(self, connection_id, done=None):
        def answered(payload):
            self._heard(connection_id, payload)
            for one in self.connections:
                if one.get("id") == connection_id:
                    one["last_check"] = {
                        "at": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                        "ok": bool(payload.get("ok")),
                        "account": (payload.get("result") or {}).get("account", ""),
                        "error": payload.get("error", "")}
            self.connections_tab.set_connections(self.connections, self.health)
            self._show_health()
            if done is not None:
                done(payload)

        self._call("test", lambda core, cancel: core.integrations(
            "test", connection_id, cancel=cancel),
                   answered, quiet=True)

    def sign_in(self, request, done, cancel):
        """Sign in through the browser. Waits on Atlassian, not on Jira: the
        process listens for the browser coming back, up to five minutes, and
        ``cancel`` ends it."""
        def answered(payload):
            if payload.get("ok") and payload.get("connection"):
                saved = payload["connection"]
                self.health.pop(saved.get("id"), None)
                self._set_connections([saved if one.get("id") == saved.get("id") else one
                                       for one in self.connections], pick=saved.get("id"))
            done(payload)

        self._call("sign-in", lambda core, cancel: core.integrations(
            "sign-in", document=dict(request), timeout=330, cancel=cancel),
            answered, quiet=True, cancel=cancel)

    def open_app(self, parent=None):
        """The project's Atlassian OAuth app: read (a file, no request), then shown."""
        from .jiraconnections import OAuthAppDialog

        def loaded(payload):
            dialog = OAuthAppDialog(parent or self, payload.get("app") or {})
            dialog.save_requested.connect(
                lambda document, done: self._call(
                    "app-save", lambda core, cancel: core.integrations(
                        "app-save", document=document, cancel=cancel), done, quiet=True))
            dialog.exec()
            dialog.deleteLater()

        self._call("app", lambda core, cancel: core.integrations("app", cancel=cancel),
                   loaded, quiet=True)

    def remove_connection(self, connection_id, done=None):
        def answered(payload):
            if payload.get("ok"):
                self.cache.pop(connection_id, None)
                self.health.pop(connection_id, None)
                self._set_connections([one for one in self.connections
                                       if one.get("id") != connection_id])
            if done is not None:
                done(payload)

        self._call("remove", lambda core, cancel: core.integrations(
            "delete", connection_id, cancel=cancel),
                   answered, quiet=True)


def _done_text(operation, result):
    """What the line under the issue says once a change is made."""
    issue = result.get("issue") or {}
    return {"transition": "Moved to %s." % (issue.get("status") or "its new status"),
            "comment": "Comment posted.", "edit": "Saved.",
            "assign": "Assigned to %s." % (issue.get("assignee") or "nobody"),
            "attach": "File attached.", "create": "%s created." % issue.get("key", "Issue"),
            }.get(operation, "")


def _moved(connections, saved):
    """Whether a save changed where or as whom: what was loaded is someone else's."""
    before = next((one for one in connections if one.get("id") == saved.get("id")), None)
    if before is None:
        return False
    return any(before.get(key) != saved.get(key)
               for key in ("site", "flavour", "auth", "email", "verify_tls"))
