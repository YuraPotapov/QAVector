"""Search: Ctrl+K, from any window, over everything the application lists.

There is no Search button anywhere and none is wanted - the person who looks
for a service, a scenario, a cycle run or an account already knows its name, and
the way to it is a key and a few letters. :class:`SearchPopup` is the same
popup as the one that clarifies a running cycle (``cycleinteractions``): the
same frame over whatever window is in front, the same head band, the same keys
line where a button would be.

What it searches is what the pages list - names, ids, keys, tags, descriptions,
statuses - and never a secret. Each page answers for its own records
(``search_rows()``) and knows how to put one on screen (``show_found(target)``);
this module only matches and shows. Results come grouped by section, in the
rail's order, and anything with a state - a service running or failed, a cycle
listening, a run going - says so, which is also what an empty field shows: what
is active now.

A page may also offer folders whose files are worth reading
(``search_documents()``): the Artifacts page offers its runs, so a task is found
by what its reading copy says, not only by the run's name. That is read off the
GUI thread, a moment after typing stops (:mod:`cms_gui.contentsearch`), one row
per document with how often the words occur in it - pages first, then the rest.

**Ctrl+K is a shortcut text fields let through.** It is a QAction on the
main window, so it works from every window and a modal dialog keeps its keys.
On X11, though, Qt gives text fields Ctrl+K as "delete to the end of the line",
and a field with focus wins over any shortcut - so the key would open Search
everywhere except where people type. :class:`KeyCatcher`, on the fields
themselves, tells them it is not theirs.
"""

import collections
import logging
import re
import weakref

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QAbstractSpinBox, QApplication,
                               QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPlainTextEdit, QTextEdit,
                               QVBoxLayout)

from . import background, widgets
from .hotkeys import registry_for, sequence_from_event

#: What opens Search, from any window of the application.
SHORTCUT = "Ctrl+K"

#: One thing found: where it lives (a page's key), how it reads, what state it
#: is in and in which tone (a :class:`widgets.Tag` variant), what else it may
#: be found by, what the page needs to show it, whether it is active now - and
#: the project it is in, when that is not the one on screen ("" when it is).
Hit = collections.namedtuple(
    "Hit", "section title detail status tone keys target live project",
    defaults=("",))
#: One section of the results: its page's key, and its hits, best first.
Group = collections.namedtuple("Group", "section hits")

#: How many hits a section shows before "N more".
PER_SECTION = 6


def hit(title, detail="", status="", tone="neutral", keys="", target=None,
        live=False, section=""):
    """A :class:`Hit`, as a page makes one - the window fills in the section."""
    return Hit(section, str(title or ""), str(detail or ""), str(status or ""),
               tone or "neutral", str(keys or ""), target, bool(live))


# -- matching -----------------------------------------------------------------
def words(query):
    return [word for word in (query or "").casefold().split() if word]


def score(found, wanted):
    """How well ``found`` answers ``wanted`` - lower is better; None for not at all.

    Every word has to be somewhere. Then: the title starting with the query,
    each word starting a word of the title, the title holding every word, and
    last the words found only in what else the hit is known by.
    """
    title = found.title.casefold()
    rest = " ".join((found.detail, found.status, found.keys)).casefold()
    if not all(word in title or word in rest for word in wanted):
        return None
    if title.startswith(" ".join(wanted)):
        return 0
    starts = [part for part in re.split(r"[^\w]+", title) if part]
    if all(any(part.startswith(word) for part in starts) for word in wanted):
        return 1
    if all(word in title for word in wanted):
        return 2
    return 3


def find(hits, query, order=()):
    """The hits matching ``query``, grouped by section in ``order``, best first."""
    wanted = words(query)
    if not wanted:
        return []
    groups = collections.OrderedDict((section, []) for section in order)
    for index, found in enumerate(hits):
        rank = score(found, wanted)
        if rank is not None:
            groups.setdefault(found.section, []).append((rank, index, found))
    return [Group(section, [found for _rank, _index, found in sorted(rows,
                                                                     key=lambda r: r[:2])])
            for section, rows in groups.items() if rows]


def active(hits, order=()):
    """What an empty field shows: everything with a state worth knowing now."""
    groups = collections.OrderedDict((section, []) for section in order)
    for found in hits:
        if found.live:
            groups.setdefault(found.section, []).append(found)
    return [Group(section, rows) for section, rows in groups.items() if rows]


# -- the popup ----------------------------------------------------------------
class _HitRow(QFrame):
    """One hit: what it is on top, where it is under it, its state on the right."""

    def __init__(self, found, parent=None):
        super().__init__(parent)
        self.setProperty("role", "searchhit")
        self.setProperty("current", "false")
        self.setAttribute(Qt.WA_Hover, True)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 5, 12, 5)
        row.setSpacing(10)
        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(1)
        # Made with their parent: a label shown before it has one is a window
        # of its own for a moment, and a window appearing closes the popup.
        self.title = widgets.ElidedLabel(mode=Qt.ElideRight, parent=self)
        self.title.setText(found.title)
        self.title.setProperty("role", "searchtitle")
        text.addWidget(self.title)
        self.detail = widgets.ElidedLabel(mode=Qt.ElideMiddle, parent=self)
        self.detail.setText(found.detail)
        self.detail.setProperty("role", "searchdetail")
        text.addWidget(self.detail)
        self.detail.setVisible(bool(found.detail))
        row.addLayout(text, 1)
        self.tag = None
        if found.status:
            if found.tone == "accent" and found.live:
                row.addWidget(widgets.PulseDot())
            self.tag = widgets.Tag(found.status, found.tone)
            row.addWidget(self.tag)

    def set_current(self, current):
        value = "true" if current else "false"
        if self.property("current") != value:
            self.setProperty("current", value)
            for widget in (self, self.title, self.detail):
                widget.style().unpolish(widget)
                widget.style().polish(widget)


class SearchPopup(widgets.FloatingPanel):
    """The field and what it finds, over whatever window is in front."""

    #: A hit was chosen - Enter, or a click.
    opened = Signal(object)

    #: How wide it is, and how far below the top of the window it opens - the
    #: clarify popup's own numbers, so the two are one family.
    WIDTH = 760
    DROP = 96
    #: The tallest the list grows before it scrolls.
    LIST_HEIGHT = 440
    #: How often the states shown are read again while it is open.
    TICK_MS = 1000
    #: How long typing has to pause before the files are read.
    FILES_AFTER_MS = 300

    def __init__(self, collect, sections, parent=None, documents=None):
        super().__init__(parent)
        self.setProperty("role", "clarifypopup")
        self.setFixedWidth(self.WIDTH)
        self._collect = collect
        self._sections = collections.OrderedDict(sections)
        self._hits = []
        self._expanded = set()
        self._shown = None
        self._rows = []                  # (item, widget, hit or a section to expand)
        # What the files say: ``documents()`` answers [(section, roots)]; the
        # documents found so far, and which query they answer. Each query is a
        # generation - a pass still reading for an older one stops and is
        # dropped. ``_run`` is background.run, which the tests replace.
        self._documents = documents
        self._run = background.run
        self._documents_found = []
        self._documents_query = ""
        self._reading = False
        self._generation = 0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        head = QFrame()
        head.setProperty("role", "clarifyhead")
        band = QHBoxLayout(head)
        band.setContentsMargins(12, 7, 12, 7)
        band.setSpacing(8)
        band.addWidget(widgets.kicker("Search"))
        self.summary = widgets.ElidedLabel(mode=Qt.ElideRight)
        self.summary.setProperty("role", "hint")
        band.addWidget(self.summary, 1)
        keys = QLabel("Up/Down moves  Enter opens  Esc closes")
        keys.setProperty("role", "keys")
        band.addWidget(keys)
        outer.addWidget(head)

        box = QFrame()
        box.setProperty("role", "composer")
        field_row = QVBoxLayout(box)
        field_row.setContentsMargins(12, 8, 12, 8)
        self.field = QLineEdit()
        self.field.setPlaceholderText("Services, scenarios, cycles, runs, accounts, "
                                      "words in reports...")
        self.field.installEventFilter(self)
        self.field.textChanged.connect(self._typed)
        field_row.addWidget(self.field)
        outer.addWidget(box)

        self.results = QListWidget()
        self.results.setProperty("role", "searchresults")
        self.results.setFocusPolicy(Qt.NoFocus)
        self.results.setSelectionMode(QAbstractItemView.SingleSelection)
        self.results.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.results.itemClicked.connect(self._clicked)
        self.results.currentRowChanged.connect(self._current_changed)
        outer.addWidget(self.results)
        self.empty = QLabel("")
        self.empty.setProperty("role", "hint")
        self.empty.setWordWrap(True)
        self.empty.setContentsMargins(12, 10, 12, 12)
        outer.addWidget(self.empty)

        self._tick = QTimer(self)
        self._tick.setInterval(self.TICK_MS)
        self._tick.timeout.connect(self._read_again)
        self._files_after = QTimer(self)
        self._files_after.setSingleShot(True)
        self._files_after.setInterval(self.FILES_AFTER_MS)
        self._files_after.timeout.connect(self.read_documents)

    # -- opening ---------------------------------------------------------------
    def open_over(self, window=None):
        """Read what there is, and show near the top of ``window``, ready to type."""
        window = window or QApplication.activeWindow()
        if self.isVisible():
            self.field.setFocus()
            self.field.selectAll()
            return
        self._hits = self._collect()
        self._expanded = set()
        self._shown = None
        self._forget_documents()
        self._fill(keep=False)
        self._files_after.start()
        self.adjustSize()
        if window is not None and window is not self:
            frame = window.frameGeometry()
            self.move(frame.x() + max(0, (frame.width() - self.width()) // 2),
                      frame.y() + min(self.DROP, max(0, frame.height() // 5)))
        self.show()
        self.raise_()
        self.activateWindow()
        self.field.setFocus()
        self.field.selectAll()
        self._tick.start()

    def hideEvent(self, event):               # noqa: N802 - Qt's own spelling
        self._tick.stop()
        self._files_after.stop()
        self._generation += 1            # a pass still reading stops
        super().hideEvent(event)

    def keyPressEvent(self, event):          # noqa: N802 - Qt's own spelling
        if event.key() == Qt.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)

    # -- what it shows ---------------------------------------------------------
    def groups(self):
        """The groups on show now: the matches, or what is active.

        Documents come after what the section lists by name, in the order they
        were read: pages first, then the rest.
        """
        query = self.field.text()
        order = list(self._sections)
        if not words(query):
            return active(self._hits, order)
        named = find(self._hits, query, order)
        if query != self._documents_query or not self._documents_found:
            return named
        merged = collections.OrderedDict((section, []) for section in order)
        for group in named:
            merged.setdefault(group.section, []).extend(group.hits)
        for found in self._documents_found:
            merged.setdefault(found.section, []).append(found)
        return [Group(section, rows) for section, rows in merged.items() if rows]

    def _typed(self, _text):
        self._expanded = set()
        self._forget_documents()
        self._fill(keep=False)
        self._files_after.start()

    # -- what the files say ------------------------------------------------------
    def _forget_documents(self):
        self._generation += 1
        self._documents_found = []
        self._documents_query = ""
        self._reading = False

    def read_documents(self):
        """Read the offered folders for the words in the field: pages, then the rest."""
        from . import contentsearch

        query = self.field.text()
        if self._documents is None or not contentsearch.wanted(query):
            return
        sources = [(section, [contentsearch.Root(*root) for root in roots])
                   for section, roots in self._documents() if roots]
        if not sources:
            return
        self._forget_documents()
        self._documents_query = query
        self._reading = True
        self._read_pass(sources, query, self._generation,
                        (contentsearch.PAGES, contentsearch.FILES))
        self._fill(keep=True)

    def _read_pass(self, sources, query, generation, kinds):
        from . import contentsearch

        def stale():
            return generation != self._generation

        def work():
            return [found._replace(section=section) for section, roots in sources
                    for found in contentsearch.scan(roots, query, kinds[0], stale)]

        def done(found, error):
            if stale():
                return
            if error is not None:
                logging.warning("Search could not read the files: %s", error)
            self._documents_found.extend(found or [])
            if len(kinds) > 1 and error is None:
                self._read_pass(sources, query, generation, kinds[1:])
            else:
                self._reading = False
            self._fill(keep=True)

        self._run(work, done, owner=self)

    def _read_again(self):
        """The states shown are live: read them again, keeping the place."""
        self._hits = self._collect()
        self._fill(keep=True)

    def _fill(self, keep):
        groups = self.groups()
        shown = []
        for group in groups:
            limit = None if group.section in self._expanded else PER_SECTION
            shown.append((group.section, tuple(group.hits[:limit]),
                          max(0, len(group.hits) - PER_SECTION) if limit else 0))
        signature = [(section, tuple((h.title, h.detail, h.status, h.tone, h.live)
                                     for h in hits), more)
                     for section, hits, more in shown]
        if keep and signature == self._shown:
            self._say(groups, sum(len(group.hits) for group in groups))
            return
        current = self._current_key() if keep else None
        self._shown = signature
        self.results.clear()
        self._rows = []
        for section, hits, more in shown:
            self._add_heading(self._sections.get(section, section))
            for found in hits:
                self._add_row(_HitRow(found), found)
            if more:
                label = QLabel("%d more in %s" % (more, self._sections.get(section, section)))
                label.setProperty("role", "searchmore")
                label.setContentsMargins(12, 4, 12, 6)
                self._add_row(label, section)
        count = sum(len(group.hits) for group in groups)
        self._say(groups, count)
        self.results.setVisible(bool(self._rows))
        self.results.setFixedHeight(min(self.LIST_HEIGHT, self._list_height()))
        self.adjustSize()
        if not self._select_key(current):
            self._move_to(self._first_selectable(), 1)

    def _say(self, groups, count):
        query = self.field.text().strip()
        sections = len(groups)
        if query:
            found = ("%d found in %d section%s" % (
                count, sections, "" if sections == 1 else "s") if count else "")
            reading = "reading files..." if self._reading else ""
            self.summary.setText(" · ".join(part for part in (found, reading) if part))
            self.empty.setText("" if count else
                               "Reading files for \"%s\"..." % query if self._reading
                               else "Nothing matches \"%s\"." % query)
        else:
            self.summary.setText("%d active now" % count if count else "")
            self.empty.setText("" if count else
                               "Type to search every section: services, scenarios, "
                               "cycles and their runs, accounts, environments, "
                               "launch configurations, runs and their reports.")
        self.empty.setVisible(bool(self.empty.text()))

    def _add_heading(self, text):
        item = QListWidgetItem()
        item.setFlags(Qt.NoItemFlags)
        label = widgets.kicker(text)
        label.setContentsMargins(12, 8, 12, 2)
        item.setSizeHint(label.sizeHint())
        self.results.addItem(item)
        self.results.setItemWidget(item, label)

    def _add_row(self, widget, payload):
        item = QListWidgetItem()
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        item.setSizeHint(widget.sizeHint())
        self.results.addItem(item)
        self.results.setItemWidget(item, widget)
        self._rows.append((item, widget, payload))

    def _list_height(self):
        rows = self.results.count()
        return (sum(self.results.sizeHintForRow(index) for index in range(rows))
                + 2 * self.results.frameWidth() + 4)

    # -- moving, and choosing ---------------------------------------------------
    def _payload(self, row):
        item = self.results.item(row)
        for candidate, _widget, payload in self._rows:
            if candidate is item:
                return payload
        return None

    def _selectable(self, row):
        item = self.results.item(row)
        return item is not None and bool(item.flags() & Qt.ItemIsSelectable)

    def _first_selectable(self):
        for row in range(self.results.count()):
            if self._selectable(row):
                return row
        return -1

    def _move_to(self, row, direction):
        while 0 <= row < self.results.count() and not self._selectable(row):
            row += direction
        if 0 <= row < self.results.count():
            self.results.setCurrentRow(row)
            self.results.scrollToItem(self.results.item(row))

    def _move(self, steps):
        row = self.results.currentRow()
        direction = 1 if steps > 0 else -1
        for _ in range(abs(steps)):
            following = row + direction
            while 0 <= following < self.results.count() and not self._selectable(following):
                following += direction
            if not 0 <= following < self.results.count():
                break
            row = following
        self._move_to(row, direction)

    def _current_changed(self, row):
        for item, widget, _payload in self._rows:
            if isinstance(widget, _HitRow):
                widget.set_current(item is self.results.item(row))

    def _current_key(self):
        payload = self._payload(self.results.currentRow())
        if isinstance(payload, Hit):
            return ("hit", payload.section, repr(payload.target))
        return ("more", payload) if payload is not None else None

    def _select_key(self, key):
        if key is None:
            return False
        for row in range(self.results.count()):
            payload = self._payload(row)
            here = (("hit", payload.section, repr(payload.target))
                    if isinstance(payload, Hit) else ("more", payload))
            if payload is not None and here == key:
                self._move_to(row, 1)
                return True
        return False

    def activate(self, row=None):
        """Open the hit at ``row`` (the current one by default), or expand a section."""
        payload = self._payload(self.results.currentRow() if row is None else row)
        if isinstance(payload, Hit):
            self.hide()
            self.opened.emit(payload)
        elif payload is not None:
            self._expanded.add(payload)
            self._fill(keep=False)
            self._select_section_end(payload)

    def _select_section_end(self, section):
        """After "N more": stay where the reader was - on the first of the rest."""
        rows = [row for row in range(self.results.count())
                if isinstance(self._payload(row), Hit)
                and self._payload(row).section == section]
        if len(rows) > PER_SECTION:
            self._move_to(rows[PER_SECTION], 1)

    def _clicked(self, item):
        self.activate(self.results.row(item))

    def eventFilter(self, watched, event):     # noqa: N802 - Qt's own spelling
        """The field keeps the keys; these few move over the list instead."""
        if watched is self.field and event.type() == QEvent.KeyPress:
            key = event.key()
            if _needs_search_override(event, self):
                # The Search key again: the query, ready to type over -
                # never the field's own "delete to the end of the line".
                self.field.selectAll()
                return True
            if key == Qt.Key_Down:
                self._move(1)
                return True
            if key == Qt.Key_Up:
                self._move(-1)
                return True
            if key == Qt.Key_PageDown:
                self._move(5)
                return True
            if key == Qt.Key_PageUp:
                self._move(-5)
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                self.activate()
                return True
            if key == Qt.Key_Escape:
                self.hide()
                return True
        return super().eventFilter(watched, event)


# -- the key ------------------------------------------------------------------
#: The widgets that answer Ctrl+K themselves on X11 - "delete to the end of the
#: line" - and so would keep the shortcut from firing while they have focus. A
#: spin box or a combo box answers for the line edit inside it.
FIELDS = (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox, QComboBox)


def _search_action(widget):
    """Find the owning main window, also from its detached child windows."""
    owner = widget
    while owner is not None:
        action = getattr(owner, "search_action", None)
        if action is not None:
            return action
        # Small standalone hosts (including plugins) can expose their action
        # without implementing the main window's attribute convention.
        if hasattr(owner, "actions") and callable(owner.actions):
            for action in owner.actions():
                if isinstance(action, QAction) and action.text().replace("&", "") == "Search":
                    return action
        owner = owner.parent()
    for window in QApplication.topLevelWidgets():
        action = getattr(window, "search_action", None)
        if action is not None and window.isVisible():
            return action
    return None


def _is_shortcut(event, widget=None):
    registry = registry_for(widget) if widget is not None else None
    if registry is not None and registry.suspended:
        return False
    action = _search_action(widget) if widget is not None else None
    if action is not None:
        if not action.isEnabled():
            return False
        sequence = action.shortcut()
    elif registry is not None:
        sequence = registry.shortcut("application.search")
    else:
        sequence = QKeySequence(SHORTCUT)
    return (not sequence.isEmpty()
            and QKeySequence(sequence_from_event(event), QKeySequence.PortableText) == sequence)


def _needs_search_override(event, widget):
    # Ctrl+K was deliberately reserved for Search before keys were configurable.
    # Other assignments use Qt's ordinary shortcut precedence: an editor keeps
    # its Copy/Paste/Undo and navigation keys even if Search uses the same chord.
    # Asked of every key typed in every field: the cheap test goes first, and
    # the walk up to the window's Search action only for Ctrl+K itself.
    return (QKeySequence(sequence_from_event(event), QKeySequence.PortableText)
            == QKeySequence(SHORTCUT)
            and _is_shortcut(event, widget))


class KeyCatcher(QObject):
    """Lets the configured Search combination past a text field.

    The shortcut itself is a QAction on the main window (ApplicationShortcut),
    so it works from every window and a modal dialog keeps the keys, as with
    every other shortcut. What it cannot beat on its own is a focused text
    field: on X11 the field answers the ShortcutOverride for Ctrl+K with "mine"
    and deletes to the end of its line instead. This answers that first, with
    "not mine" - outside a modal dialog, where the field's meaning stays.

    Installed on the fields one by one - by :func:`catch_ctrl_k` for those that
    exist and by the application style as new ones are polished - and **never
    on the application**: an application-wide Python event filter crashes
    PySide6 the moment WebEngine delivers events for its private objects, which
    is opening any HTML page.
    """

    def __init__(self, app):
        super().__init__(app)
        self._watched = weakref.WeakSet()

    def watch(self, widget):
        if isinstance(widget, FIELDS) and widget not in self._watched:
            widget.installEventFilter(self)
            self._watched.add(widget)

    def eventFilter(self, watched, event):     # noqa: N802 - Qt's own spelling
        if (event.type() == QEvent.ShortcutOverride and _needs_search_override(event, watched)
                and QApplication.activeModalWidget() is None):
            event.ignore()               # not the field's: the shortcut's
            return True
        return False


def catch_ctrl_k(app):
    """Guard the fields there are now; the application style guards new ones."""
    catcher = getattr(app, "_search_keys", None)
    if catcher is None:
        catcher = KeyCatcher(app)
        app._search_keys = catcher
    for widget in app.allWidgets():
        catcher.watch(widget)
    return catcher
