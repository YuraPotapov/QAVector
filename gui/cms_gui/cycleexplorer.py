"""The Cycles page's explorer: projects, and the cycles in them.

Split out of ``pages/cycles.py`` because it is a whole thing of its own - a
tree, how its rows are drawn, its keys and its menus - that talks to the page
only through signals. The page holds the projects and the cycles and writes
every file; nothing here writes anything.
"""

import time

from PySide6.QtCore import QEvent, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QLineEdit,
                               QMenu, QPushButton, QStackedWidget, QStyle,
                               QStyledItemDelegate, QStyleOptionViewItem,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

from . import icons, theme, widgets

#: Roles on an explorer row. Qt's user role plus one, so the two things a row
#: has to carry - what kind of row it is and which cycle it names - do not have
#: to be encoded into its text.
ROLE_KIND = Qt.UserRole
ROLE_ID = Qt.UserRole + 1
#: The second line of a row - see _RowDelegate - and whether it carries bad
#: news ("bad", "warn" or "").
ROLE_META = Qt.UserRole + 2
ROLE_ALERT = Qt.UserRole + 3
#: The whole inventory row a cycle's item stands for.
ROLE_ROW = Qt.UserRole + 4

#: What the group holding cycles that name no project - or one nothing knows -
#: is called. Named rather than hidden, the way the Services page handles a log
#: whose stack has gone: losing sight of a cycle because its project was
#: renamed would be the worst possible answer.
UNASSIGNED = "Unassigned"


class _RowDelegate(QStyledItemDelegate):
    """An explorer row: its name, and under it one line worth knowing.

    A tree of bare names made every cycle look alike - which one is the long
    one, which one ships with the application, which one will not run, which
    one ran an hour ago and failed were all a hover or a click away. The second
    line says those things in words, in the muted ink the rest of the page
    uses for anything secondary, so the name stays what the eye lands on.

    Colours are read at paint time, never kept: dark mode rewrites ``theme``
    in place, and the selected row's ink is the stylesheet's selection ink.
    """

    #: Pixel sizes of the two lines. The name matches the tree's own text.
    TITLE = 13
    META = 11
    #: Room above, between and below the two lines.
    PAD = 6
    GAP = 1

    def paint(self, painter, option, index):
        view = QStyleOptionViewItem(option)
        self.initStyleOption(view, index)
        title, meta = view.text, index.data(ROLE_META) or ""
        view.text = ""
        widget = view.widget
        style = widget.style() if widget is not None else QApplication.style()
        # The row itself - background, selection, focus, icon - is the style's,
        # so a selected row looks like a selected row everywhere else.
        style.drawControl(QStyle.CE_ItemViewItem, view, painter, widget)
        rect = style.subElementRect(QStyle.SE_ItemViewItemText, view, widget)
        selected = bool(view.state & QStyle.State_Selected)
        kind = index.data(ROLE_KIND)

        painter.save()
        name_font = QFont(view.font)
        name_font.setPixelSize(self.TITLE)
        if kind in ("project", "unlisted", "unassigned"):
            name_font.setWeight(QFont.DemiBold)
        if kind in ("unlisted", "empty"):
            name_font.setItalic(True)
        meta_font = QFont(view.font)
        meta_font.setPixelSize(self.META)
        name_height = QFontMetrics(name_font).height()
        meta_height = QFontMetrics(meta_font).height() if meta else 0
        top = rect.top() + (rect.height() - name_height
                            - (meta_height + self.GAP if meta else 0)) / 2.0

        foreground = index.data(Qt.ForegroundRole)
        ink = (theme.SELECT_INK if selected else
               foreground.color().name() if foreground is not None else theme.TEXT)
        painter.setFont(name_font)
        painter.setPen(QColor(ink))
        painter.drawText(QRectF(rect.left(), top, rect.width(), name_height),
                         Qt.AlignLeft | Qt.AlignVCenter,
                         QFontMetrics(name_font).elidedText(title, Qt.ElideRight,
                                                            rect.width()))
        if meta:
            alert = index.data(ROLE_ALERT)
            painter.setFont(meta_font)
            painter.setPen(QColor(
                theme.SELECT_MUTED if selected
                else theme.BAD if alert == "bad"
                else theme.WARN if alert == "warn" else theme.NEUTRAL[700]))
            painter.drawText(
                QRectF(rect.left(), top + name_height + self.GAP, rect.width(),
                       meta_height),
                Qt.AlignLeft | Qt.AlignVCenter,
                QFontMetrics(meta_font).elidedText(meta, Qt.ElideRight,
                                                   rect.width()))
        painter.restore()

    def sizeHint(self, option, index):
        size = QStyledItemDelegate.sizeHint(self, option, index)
        name = QFontMetrics(QFont(option.font)).height()
        extra = (QFontMetrics(QFont(option.font)).height() + self.GAP
                 if index.data(ROLE_META) else 0)
        size.setHeight(max(size.height(), name + extra + 2 * self.PAD))
        return size


class Explorer(QWidget):
    """The projects, and the cycles in them.

    A tree from the start rather than a list, because there will be more in it:
    Runs and Artifacts belong here too eventually, and a list would have to
    become a tree later and every call site with it.

    It does not own anything - the page holds the projects and the cycles, and
    every action here is a signal. That keeps the one thing that writes files in
    one place, which is what stops two halves of a page disagreeing about what
    is on disk.

    **Where a cycle belongs is what its own file says.** The projects file is
    the page's list of names - what New Cycle offers, and where an empty project
    lives - not a second record of membership. So a cycle naming a project the
    list has never heard of (imported, copied from another machine, or one that
    ships with the application) is shown under *that* name, marked as not in
    the list, rather than being filed under Unassigned where the name it
    carries could not be seen at all.
    """

    opened = Signal(str)                 # a cycle id
    selection_changed = Signal()
    project_added = Signal()
    project_renamed = Signal(str)        # the old name
    project_deleted = Signal(str)
    #: A project cycles name but the list does not have: put it in the list.
    project_listed = Signal(str)
    cycle_added = Signal(str)            # the project to put it in
    cycle_duplicated = Signal(str)
    cycle_renamed = Signal(str)          # change what it is called
    cycle_reidentified = Signal(str)     # change the id, which renames the file
    cycle_deleted = Signal(str)
    cycle_imported = Signal()
    #: Move a cycle to another project: (cycle id, project, "" for none).
    cycle_moved = Signal(str, str)
    #: A person opened or shut a group: every answer so far, {name: open}.
    folds_changed = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = {}                  # cycle id -> item
        self._projects = {}              # project name -> item
        self._listed = []                # the names the projects file has
        self._cycles = []
        #: group name -> open, for every group somebody opened or shut. Only
        #: their choices: a group nobody touched opens, as it always did.
        self._folds = {}
        #: Set while fill puts the groups back, so restoring a fold is not
        #: taken for somebody making one.
        self._filling = False
        #: cycle id -> {"when": epoch seconds, "status": ...}, from the sessions.
        self._last_runs = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 12, 0)
        layout.setSpacing(8)

        # The two things somebody arriving here does first, where they can be
        # seen. Everything else stays on the right-click menu of the row it
        # acts on - a tree's actions belong to its rows.
        self.new_project_button = icons.button(QPushButton(), "folder")
        self.new_project_button.setToolTip("New project - somewhere to keep "
                                           "cycles together.")
        self.new_project_button.clicked.connect(self.project_added.emit)
        self.new_cycle_button = icons.button(QPushButton(), "plus")
        self.new_cycle_button.setToolTip(
            "New cycle, in the selected project.")
        self.new_cycle_button.clicked.connect(
            lambda: self.cycle_added.emit(self.current_project()))
        self.more_button = icons.button(QPushButton(), "more")
        self.more_button.setToolTip("More: import a cycle file.")
        self.more_menu = QMenu(self.more_button)
        self.more_menu.addAction("Import a cycle file...", self.cycle_imported.emit)
        self.more_button.clicked.connect(lambda: self.more_menu.exec(
            self.more_button.mapToGlobal(self.more_button.rect().bottomLeft())))
        for button in (self.new_project_button, self.new_cycle_button,
                       self.more_button):
            button.setProperty("variant", "ghost")
        layout.addWidget(widgets.row(widgets.kicker("Cycles"), None,
                                     self.new_project_button,
                                     self.new_cycle_button, self.more_button,
                                     spacing=0))

        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by name, id or project")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)

        #: Said when the projects file cannot be read - once, here, rather than
        #: as an empty tree that looks like nothing was ever made.
        self.error = widgets.lede("")
        self.error.setVisible(False)
        layout.addWidget(self.error)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setIndentation(14)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setItemDelegate(_RowDelegate(self.tree))
        self.tree.setTextElideMode(Qt.ElideRight)
        self.tree.itemActivated.connect(self._activated)
        self.tree.itemClicked.connect(self._activated)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.setProperty("role", "panel")
        self.tree.installEventFilter(self)
        self.tree.currentItemChanged.connect(lambda *_a: self.selection_changed.emit())
        self.tree.itemExpanded.connect(lambda item: self._folded(item, True))
        self.tree.itemCollapsed.connect(lambda item: self._folded(item, False))

        self.empty = widgets.empty_zone(
            "No cycles yet",
            "Press Cycle to write one, or import a cycle file from More.")
        self.stack = QStackedWidget()
        self.stack.addWidget(self.tree)
        self.stack.addWidget(self.empty)
        layout.addWidget(self.stack, 1)

        self.count = widgets.mono("")
        layout.addWidget(self.count)

    # -- filling it -----------------------------------------------------------
    def fill(self, cycles, projects):
        """Rebuild from the cycles and the projects, keeping what was selected.

        Both kinds of selection, not only a cycle. The tree is rebuilt whenever
        anything is written - including writing the projects file itself - so a
        project that lost its selection every time meant Rename and Delete went
        grey the instant you made the project you wanted to rename.
        """
        was_cycle, was_project = self.current_id(), self._selected_project()
        self._filling = True
        try:
            self._fill(cycles, projects)
        finally:
            self._filling = False
        if was_cycle:
            self.select(was_cycle)
        elif was_project:
            self.select_project(was_project)
        self._filter(self.search.text())

    def _fill(self, cycles, projects):
        self.tree.clear()
        self._rows, self._projects = {}, {}
        self._cycles = list(cycles)

        self._listed = [project.name for project in projects if project.name]
        listed = {name.casefold(): name for name in self._listed}
        grouped = {name: [] for name in self._listed}
        unlisted = {}
        for row in cycles:
            name = (row.get("project") or "").strip()
            if not name:
                grouped.setdefault(UNASSIGNED, []).append(row)
            elif name.casefold() in listed:
                grouped[listed[name.casefold()]].append(row)
            else:
                unlisted.setdefault(name, []).append(row)

        for name in self._listed:
            self._group(name, grouped.get(name) or [], "project")
        for name in sorted(unlisted, key=str.casefold):
            self._group(name, unlisted[name], "unlisted")
        # Only when there is something in it: an empty Unassigned group is a
        # question nobody asked.
        if grouped.get(UNASSIGNED):
            self._group(UNASSIGNED, grouped[UNASSIGNED], "unassigned")

        self.stack.setCurrentWidget(self.tree if (cycles or self._listed)
                                    else self.empty)

    # -- which groups are open ------------------------------------------------
    def set_folds(self, states):
        """Which groups somebody opened or shut before, as {name: open}.

        A view preference, kept by the page the way Services & Logs keeps its
        own (``Settings.folds``): folding Work Projects shut once should not
        have to be done again after every restart.
        """
        self._folds = {str(name): bool(value)
                       for name, value in dict(states or {}).items()}
        for name, item in self._projects.items():
            self._filling = True
            try:
                item.setExpanded(self._folds.get(name, True))
            finally:
                self._filling = False

    def folds(self):
        return dict(self._folds)

    def forget_fold(self, name, new_name=None):
        """A project renamed (its fold moves along) or deleted (it goes)."""
        if name not in self._folds:
            return
        value = self._folds.pop(name)
        if new_name:
            self._folds[new_name] = value
        self.folds_changed.emit(dict(self._folds))

    def _folded(self, item, expanded):
        if self._filling or item.data(0, ROLE_KIND) not in (
                "project", "unlisted", "unassigned"):
            return
        name = item.text(0)
        if self._folds.get(name) == expanded:
            return
        self._folds[name] = expanded
        self.folds_changed.emit(dict(self._folds))

    def set_error(self, text):
        """Say the projects file could not be read, or stop saying it."""
        self.error.setText(text or "")
        self.error.setVisible(bool(text))
        self.error.setStyleSheet("color: %s;" % theme.BAD if text else "")

    def set_last_runs(self, last_runs):
        """What each cycle last did, by id, for the second line of its row."""
        self._last_runs = dict(last_runs or {})
        for cycle_id, item in self._rows.items():
            self._describe(item, item.data(0, ROLE_ROW) or {})

    def _group(self, name, rows, kind):
        item = QTreeWidgetItem(self.tree, [name])
        item.setData(0, ROLE_KIND, kind)
        item.setData(0, ROLE_ID, name if kind != "unassigned" else "")
        item.setIcon(0, icons.icon("folder", theme.NEUTRAL[600]))
        count = "%d cycle%s" % (len(rows), "" if len(rows) == 1 else "s")
        if kind == "unlisted":
            item.setData(0, ROLE_META, "not in your projects list - %s" % count)
            item.setToolTip(0, "%d cycle(s) name %r, which your projects list "
                            "does not have. Right-click to add it." % (len(rows), name))
        elif kind == "unassigned":
            item.setData(0, ROLE_META, "%s with no project" % count)
            item.setToolTip(0, "Cycles whose file names no project.")
        else:
            item.setData(0, ROLE_META, count if rows else "empty")
            item.setToolTip(0, count)
        self._projects[name] = item

        if not rows:
            empty = QTreeWidgetItem(item, ["No cycles yet"])
            empty.setData(0, ROLE_KIND, "empty")
            empty.setFlags(Qt.ItemIsEnabled)
            empty.setForeground(0, theme.color(theme.NEUTRAL[600]))
        for row in sorted(rows, key=lambda one: (one.get("name")
                                                 or one.get("id", "")).lower()):
            self._leaf(item, row)
        item.setExpanded(self._folds.get(name, True))

    def _leaf(self, parent, row):
        item = QTreeWidgetItem(parent, [row.get("name") or row.get("id", "")])
        item.setData(0, ROLE_KIND, "cycle")
        item.setData(0, ROLE_ID, row.get("id", ""))
        item.setData(0, ROLE_ROW, dict(row))
        if row.get("problems"):
            item.setIcon(0, icons.icon("warn", theme.BAD))
        else:
            item.setIcon(0, icons.icon("cycles", theme.NEUTRAL[600]))
        if not row.get("writable"):
            item.setForeground(0, theme.color(theme.NEUTRAL[600]))
        self._describe(item, row)
        self._rows[row.get("id", "")] = item

    def _describe(self, item, row):
        """The second line and the tooltip: size, origin, trouble, last run."""
        steps = int(row.get("steps") or 0)
        parts = ["%d step%s" % (steps, "" if steps == 1 else "s")]
        problems = row.get("problems") or []
        alert = ""
        if problems:
            parts.insert(0, "%d problem%s" % (len(problems),
                                              "" if len(problems) == 1 else "s"))
            alert = "bad"
        if not row.get("writable"):
            parts.append("built in")
        last = self._last_runs.get(row.get("id", ""))
        if last:
            parts.append("%s %s" % (_ago(last.get("when")), last.get("status") or ""))
            if not alert and last.get("status") in ("failed", "timeout",
                                                    "interrupted", "error"):
                alert = "warn"
        item.setData(0, ROLE_META, " - ".join(one.strip() for one in parts if one.strip()))
        item.setData(0, ROLE_ALERT, alert)
        tip = ["%s  (%s)" % (row.get("name") or row.get("id", ""), row.get("id", ""))]
        if row.get("description"):
            tip.append(row["description"])
        tip.extend(str(one) for one in problems[:6])
        item.setToolTip(0, "\n".join(tip))

    # -- what is selected -----------------------------------------------------
    def select(self, cycle_id):
        item = self._rows.get(cycle_id)
        if item is not None and item is not self.tree.currentItem():
            self.tree.setCurrentItem(item)

    def select_project(self, name):
        item = self._projects.get(name)
        if item is not None and item is not self.tree.currentItem():
            self.tree.setCurrentItem(item)

    def _selected_project(self):
        """The project row that is selected, if it is a project row itself.

        Not :meth:`current_project`, which walks up from a cycle to the project
        holding it - restoring that would move the selection off the cycle and
        onto its group.
        """
        item = self.tree.currentItem()
        if item is not None and item.data(0, ROLE_KIND) in ("project", "unlisted"):
            return item.data(0, ROLE_ID) or ""
        return ""

    def current_id(self):
        item = self.tree.currentItem()
        if item is not None and item.data(0, ROLE_KIND) == "cycle":
            return item.data(0, ROLE_ID) or ""
        return ""

    def current_project(self):
        """Which project a New Cycle should go into, from what is selected."""
        item = self.tree.currentItem()
        while item is not None:
            if item.data(0, ROLE_KIND) in ("project", "unlisted"):
                return item.data(0, ROLE_ID) or ""
            item = item.parent()
        return ""

    def current_kind(self):
        item = self.tree.currentItem()
        return item.data(0, ROLE_KIND) if item is not None else ""

    def set_developer_mode(self, enabled):
        pass          # nothing here is developer-only yet

    # -- acting on it ---------------------------------------------------------
    def _activated(self, item, _column=0):
        if item is not None and item.data(0, ROLE_KIND) == "cycle":
            self.opened.emit(item.data(0, ROLE_ID) or "")

    def eventFilter(self, watched, event):
        """The keys a tree is expected to answer, besides moving and Enter.

        F2 renames, Delete deletes - both through the same questions the menu
        asks - and the menu key opens the row's menu where the row is, rather
        than wherever the mouse happens to be resting.
        """
        if watched is self.tree and event.type() == QEvent.KeyPress:
            item = self.tree.currentItem()
            kind = item.data(0, ROLE_KIND) if item is not None else ""
            name = item.data(0, ROLE_ID) if item is not None else ""
            key = event.key()
            if key == Qt.Key_F2 and kind == "cycle":
                self.cycle_renamed.emit(name)
                return True
            if key == Qt.Key_F2 and kind == "project":
                self.project_renamed.emit(name)
                return True
            if key == Qt.Key_Delete and kind == "cycle":
                self.cycle_deleted.emit(name)
                return True
            if key == Qt.Key_Delete and kind == "project":
                self.project_deleted.emit(name)
                return True
            if key == Qt.Key_Menu or (key == Qt.Key_F10
                                      and event.modifiers() & Qt.ShiftModifier):
                rect = (self.tree.visualItemRect(item) if item is not None
                        else self.tree.viewport().rect())
                self._menu(rect.bottomLeft() if item is not None else rect.center())
                return True
        return QWidget.eventFilter(self, watched, event)

    def _menu(self, point):
        item = self.tree.itemAt(point)
        kind = item.data(0, ROLE_KIND) if item is not None else ""
        menu = QMenu(self)

        if kind == "cycle":
            cycle_id = item.data(0, ROLE_ID)
            row = item.data(0, ROLE_ROW) or {}
            menu.addAction("Open", lambda: self.opened.emit(cycle_id))
            menu.addSeparator()
            menu.addAction("Rename cycle...\tF2",
                           lambda: self.cycle_renamed.emit(cycle_id))
            # Its own entry, and worded as what it is. The id is the filename
            # and what --cycle-run is given; renaming is changing the label on
            # the row you just right-clicked. Offering one as the other is how
            # somebody ends up with a cycle called "Nightly validation" whose
            # id is "nightly_validation_2".
            menu.addAction("Change id (%s)..." % cycle_id,
                           lambda: self.cycle_reidentified.emit(cycle_id))
            self._move_menu(menu, cycle_id, row)
            menu.addAction("Duplicate...",
                           lambda: self.cycle_duplicated.emit(cycle_id))
            menu.addAction("Delete...\tDel",
                           lambda: self.cycle_deleted.emit(cycle_id))
            menu.addSeparator()
        elif kind in ("project", "unlisted"):
            name = item.data(0, ROLE_ID)
            menu.addAction("New cycle here...",
                           lambda: self.cycle_added.emit(name))
            menu.addSeparator()
            if kind == "unlisted":
                menu.addAction("Add to the projects list",
                               lambda: self.project_listed.emit(name))
            else:
                menu.addAction("Rename project...\tF2",
                               lambda: self.project_renamed.emit(name))
                menu.addAction("Delete project...\tDel",
                               lambda: self.project_deleted.emit(name))
            menu.addSeparator()

        menu.addAction("New project...", self.project_added.emit)
        menu.addAction("New cycle...",
                       lambda: self.cycle_added.emit(self.current_project()))
        menu.addAction("Import a cycle file...", self.cycle_imported.emit)
        menu.exec(self.tree.viewport().mapToGlobal(point))

    def _move_menu(self, menu, cycle_id, row):
        """"Move to project": every project but the one it is in, and none."""
        where = (row.get("project") or "").strip()
        into = menu.addMenu("Move to project")
        names = list(self._listed)
        if where and where.casefold() not in {one.casefold() for one in names}:
            names.append(where)
        for name in names:
            action = into.addAction(name, lambda n=name: self.cycle_moved.emit(cycle_id, n))
            action.setEnabled(name.casefold() != where.casefold())
        if names:
            into.addSeparator()
        action = into.addAction(UNASSIGNED, lambda: self.cycle_moved.emit(cycle_id, ""))
        action.setEnabled(bool(where))
        # A bundled cycle cannot be rewritten; say so where the choice is.
        into.setEnabled(bool(row.get("writable")))
        if not row.get("writable"):
            into.setTitle("Move to project (duplicate it first)")

    def _filter(self, text):
        needle = (text or "").strip().lower()
        shown = 0
        for cycle_id, item in self._rows.items():
            row = item.data(0, ROLE_ROW) or {}
            haystack = " ".join((cycle_id, item.text(0), row.get("description") or "",
                                 row.get("project") or "")).lower()
            hidden = bool(needle) and needle not in haystack
            item.setHidden(hidden)
            shown += not hidden
        # A project whose every cycle is filtered out goes with them, so the
        # search reads as a shorter tree rather than as a list of empty groups.
        for item in self._projects.values():
            visible = any(not item.child(index).isHidden()
                          for index in range(item.childCount())
                          if item.child(index).data(0, ROLE_KIND) == "cycle")
            item.setHidden(bool(needle) and not visible)
        total = len(self._rows)
        projects = len(self._listed)
        summary = "%d cycle%s in %d project%s" % (
            total, "" if total == 1 else "s", projects, "" if projects == 1 else "s")
        self.count.setText("%d of %s match" % (shown, summary) if needle else summary)


def _ago(when):
    """How long ago, the way a person says it: "5m ago", "3h ago", "2d ago"."""
    try:
        seconds = max(0, time.time() - float(when))
    except (TypeError, ValueError):
        return ""
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return "%dm ago" % (seconds // 60)
    if seconds < 86400:
        return "%dh ago" % (seconds // 3600)
    return "%dd ago" % (seconds // 86400)
