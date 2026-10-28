"""The accounts a launch opens, as a table: a tick, the role, the login, and the
scenarios the account carries of its own.

**The project's table.** The tick column, the "all" box over it and the inset
every title shares with its values are :mod:`cms_gui.choicetable`'s, and the
table and its search row are :mod:`cms_gui.widgets`' - the recipe the services
page and ``user.choice`` already follow, so the columns, their titles and the
search boxes over them line up the same way everywhere.

**Two ways to say "these accounts".** *All accounts* is every account of the
environment, now and later - one added tomorrow goes too - so every box is
shown ticked and none can be changed. *Choose accounts* is the ticks. What was
chosen is kept while *All* is on, and comes back when *Choose* is picked again.

**What is ticked is never hidden by what is searched.** The search hides rows;
the ticks of the rows it hides still count, and the line under the table says
how many are out of sight. *Selected* narrows the table to the ticked rows, so a
long list's selection can be checked at a glance.

**Every gap is said.** An environment with no accounts, a search that hides
every row, a Selected view with nothing ticked - each puts a sentence in the
table rather than leaving it blank; an empty cell is a dash.
"""

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QHeaderView, QStyle,
                               QStyledItemDelegate, QStyleOptionViewItem,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from .. import theme, widgets
from ..choicetable import (CHECK_WIDTH, COLUMN_CAP, EMPTY, MULTIPLE, NO_MATCH,
                           CheckHeader, indicator_rect, text_inset)

COLUMNS = ("Role", "Login", "Own scenarios")

#: What the table says when there is nothing in it, or nothing shown.
NO_ACCOUNTS = "No accounts here yet - + Account adds one."
NOTHING_SELECTED = "Nothing selected yet - tick accounts under All."
#: The note in the scenarios column of an account the environment does not have.
NOT_HERE = "not in this environment"

#: Room for the header, the search row and a handful of rows; more scroll.
MIN_HEIGHT = 190


class _TickDelegate(QStyledItemDelegate):
    """The tick column, painted the choice table's way - greyed while locked."""

    def __init__(self, owner):
        super().__init__(owner)
        self._owner = owner

    def paint(self, painter, option, index):
        if index.column() != 0:
            super().paint(painter, option, index)
            return
        view = QStyleOptionViewItem(option)
        self.initStyleOption(view, index)
        view.text = ""
        view.features &= ~QStyleOptionViewItem.HasCheckIndicator
        widget = option.widget
        if widget is not None:
            widget.style().drawControl(QStyle.CE_ItemViewItem, view, painter, widget)
        state = QStyle.State_On if self._owner.is_checked(index.row()) else QStyle.State_Off
        if not self._owner.locked:
            state |= QStyle.State_Enabled | (option.state & QStyle.State_MouseOver)
        theme.paint_box(painter, indicator_rect(option.rect), state)

    def editorEvent(self, event, model, option, index):
        if index.column() == 0 and event.type() == QEvent.MouseButtonRelease \
                and event.button() == Qt.LeftButton:
            self._owner.toggle(index.row())
            return True
        return super().editorEvent(event, model, option, index)


class _TickHeader(CheckHeader):
    """The "all" box, greyed and deaf while every account goes anyway."""

    def paintSection(self, painter, rect, logical):
        if logical != 0 or not self._owner.locked:
            super().paintSection(painter, rect, logical)
            return
        painter.save()
        QHeaderView.paintSection(self, painter, rect, logical)
        painter.restore()
        theme.paint_box(painter, indicator_rect(rect), QStyle.State_On)

    def mousePressEvent(self, event):
        if self._owner.locked and self.logicalIndexAt(event.position().toPoint()) == 0:
            return
        super().mousePressEvent(event)


class AccountsTable(QWidget):
    """See the module docstring."""

    #: What is ticked changed.
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.mode = MULTIPLE
        #: Every account goes: shown ticked, not changeable ("All accounts").
        self.locked = False
        self._rows = []
        self._checked = []

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        self.view = widgets.Segmented([widgets.VIEW_ALL, widgets.VIEW_SELECTED],
                                      widgets.VIEW_ALL)
        self.view.changed.connect(lambda _value: self._apply_visibility())
        self.count = widgets.mono("")
        column.addWidget(widgets.row(self.view, None, self.count))

        self.table = widgets.Table(0, 1 + len(COLUMNS), reclick_clears=False)
        self.header = _TickHeader(self)
        self.table.setHorizontalHeader(self.header)
        self.table.setHorizontalHeaderLabels([""] + list(COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(False)
        self.table.setProperty("rows", "quiet")
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.setMinimumHeight(MIN_HEIGHT)
        self.table.setItemDelegate(_TickDelegate(self))
        self.table.setMouseTracking(True)
        self.table.installEventFilter(self)
        self._note = widgets.empty_note(self.table, "")
        self.search = widgets.SearchRow(self.table, skip=(0,))
        self.search.changed.connect(self._apply_visibility)
        self._fit()
        # No frame of its own and no cell lines: it sits in the Accounts panel,
        # whose border already closes it in - a second one read as a box in a
        # box. The header band and the row height keep the rows apart.
        self.table.setShowGrid(False)
        column.addWidget(self.table, 1)
        self._update_count()

    # -- contents -------------------------------------------------------------
    def set_rows(self, rows, checked=()):
        """Show ``rows`` - dicts with ``login``, ``role``, ``scenarios`` (a list)
        and ``missing`` - with the logins in ``checked`` ticked."""
        wanted = set(checked or ())
        self._rows = [dict(row) for row in rows]
        self._checked = [row.get("login") in wanted for row in self._rows]
        self.table.setRowCount(len(self._rows))
        muted = theme.color(theme.NEUTRAL[500])
        for index, row in enumerate(self._rows):
            tick = QTableWidgetItem("")
            tick.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.table.setItem(index, 0, tick)
            scenarios = ", ".join(row.get("scenarios") or ())
            cells = (row.get("role") or "", row.get("login") or "",
                     NOT_HERE if row.get("missing") else scenarios)
            for offset, text in enumerate(cells, start=1):
                item = QTableWidgetItem(text or EMPTY)
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                if text:
                    item.setToolTip(text)
                if not text or (row.get("missing") and offset == 3):
                    item.setForeground(muted)
                if offset == 2:
                    item.setFont(theme.mono_font())
                self.table.setItem(index, offset, item)
        self._fit()
        self._apply_visibility()

    def _fit(self):
        """The choice table's rule: widths to the content under a ceiling, the
        tick column fixed, and the login - what a person reads first - taking
        what is left."""
        header = self.header
        header.setMinimumSectionSize(CHECK_WIDTH)
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        header.setStyleSheet("QHeaderView::section { padding-left: %dpx; }"
                             % text_inset(self.table))
        self.table.resizeColumnsToContents()
        for index in range(1, self.table.columnCount()):
            self.table.setColumnWidth(index, min(self.table.columnWidth(index),
                                                 COLUMN_CAP))
            header.setSectionResizeMode(index, QHeaderView.Interactive)
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.setColumnWidth(0, CHECK_WIDTH)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        if getattr(self, "search", None) is not None:
            self.search.place()

    def rows(self):
        return [dict(row) for row in self._rows]

    # -- ticking --------------------------------------------------------------
    def set_locked(self, locked):
        """All accounts (True): every box ticked and greyed. The pick is kept."""
        self.locked = bool(locked)
        self.table.viewport().update()
        self.header.viewport().update()
        self._apply_visibility()

    def is_checked(self, row):
        if not 0 <= row < len(self._checked):
            return False
        if self.locked:
            return not self._rows[row].get("missing")
        return self._checked[row]

    def toggle(self, row):
        if self.locked or not 0 <= row < len(self._checked):
            return
        self._set(row, not self._checked[row])
        self._changed()

    def _set(self, row, ticked):
        """Tick a row, and every row with the same login - one account."""
        login = self._rows[row].get("login")
        for index, data in enumerate(self._rows):
            if data.get("login") == login:
                self._checked[index] = bool(ticked)

    def toggle_all(self):
        if self.locked:
            return
        shown = self._shown()
        tick = not shown or not all(self._checked[row] for row in shown)
        for row in shown:
            self._set(row, tick)
        self._changed()

    def all_state(self):
        shown = self._shown()
        ticked = sum(1 for row in shown if self.is_checked(row))
        if not shown or not ticked:
            return Qt.Unchecked
        return Qt.Checked if ticked == len(shown) else Qt.PartiallyChecked

    def checked(self):
        """The ticked logins, in the table's order, each once (the pick, even
        while every account goes)."""
        found = []
        for row, ticked in enumerate(self._checked):
            login = self._rows[row].get("login")
            if ticked and login not in found:
                found.append(login)
        return found

    def set_checked(self, logins, notify=False):
        wanted = set(logins or ())
        self._checked = [row.get("login") in wanted for row in self._rows]
        self._changed(quiet=not notify)

    def set_all(self, ticked):
        if self.locked:
            return
        self._checked = [bool(ticked)] * len(self._rows)
        self._changed()

    def _changed(self, quiet=False):
        self.table.viewport().update()
        self.header.viewport().update()
        self._apply_visibility()
        if not quiet:
            self.changed.emit()

    # -- what is shown --------------------------------------------------------
    def selected_only(self):
        return self.view.current() == widgets.VIEW_SELECTED

    def _shown(self):
        return [row for row in range(self.table.rowCount())
                if not self.table.isRowHidden(row)]

    def _apply_visibility(self):
        searched = self.search.searching()
        for row in range(self.table.rowCount()):
            hide = (self.selected_only() and not self.is_checked(row)) or \
                (searched and not self.search.matches(row))
            self.table.setRowHidden(row, hide)
        if not self._rows:
            note = NO_ACCOUNTS
        elif self._shown():
            note = ""
        elif self.selected_only() and not any(self.is_checked(row)
                                              for row in range(len(self._rows))):
            note = NOTHING_SELECTED
        else:
            note = NO_MATCH
        self._note.say_filtered(note)
        self._update_count()
        self.header.viewport().update()

    def _update_count(self):
        available = len({row.get("login") for row in self._rows if not row.get("missing")})
        ticked = sum(1 for row in range(len(self._rows)) if self.is_checked(row))
        if self.locked:
            text = "All %d go" % available
        else:
            text = "Selected %d / Available %d" % (len(self.checked()), available)
        hidden = sum(1 for row in range(len(self._rows))
                     if self.table.isRowHidden(row) and self.is_checked(row))
        if hidden and not self.selected_only():
            text += "   ·   %d ticked out of sight" % hidden
        self.count.setText(text)
        self.view.set_text(widgets.VIEW_ALL, "All %d" % len(self._rows))
        self.view.set_text(widgets.VIEW_SELECTED, "Selected %d" % ticked)

    # -- keys -----------------------------------------------------------------
    def eventFilter(self, watched, event):
        if watched is self.table and event.type() == QEvent.KeyPress \
                and event.key() == Qt.Key_Space:
            row = self.table.currentRow()
            if row >= 0:
                self.toggle(row)
            return True
        return False
