"""The table a person picks rows from, for a ``user.choice`` step.

**A table, not a list.** The rows are tasks with a key, a title, a status and
somebody's name, which is four columns whatever the widget is called. Drawn as
a list with the fields padded into place by spaces they line up only until one
title is longer than the padding; drawn as a table they line up always.

**The project's table, not a new one.** :class:`widgets.Table` and
:class:`widgets.SearchRow` are the services page's, with its rules: a hairline
panel, no row numbers, whole rows selected, one column stretching, widths
fitted to the content with a ceiling, and a search box flush over each column
it searches. Two recipes for one thing is how columns and the boxes over them
stop lining up.

**The tick column is part of the grid.** It is a column like the others, the
same band in the header and the same rows - not a gutter beside the table. Its
boxes are drawn here rather than by the item view, so that every one of them,
and the "all" box in its header, is centred in its cell by the same
arithmetic: the header's box sits exactly over the boxes in its column.

**It scrolls, it does not grow.** Unlike the services page, which grows its
tables and scrolls the page, a dialog has no page to scroll: a thousand rows
stay inside the table's own scroll area, and the window keeps the height the
screen gives it.

**Every gap says what it is.** An empty cell is a dash, as on the services
page; a search that hides every row says so in the table; no row selected and
a row with no detail are each a state of the panel below, not a blank box.
"""

from PySide6.QtCore import QEvent, QRect, QRectF, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QHeaderView, QPlainTextEdit,
                               QSplitter, QStackedWidget, QStyle,
                               QStyledItemDelegate, QStyleOptionViewItem,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from . import theme, widgets

MULTIPLE, SINGLE = "multiple", "single"

#: More rows than this and every column gets a search box over it.
SEARCH_FROM = 8
#: The tick column: the box, and the same room either side of it.
CHECK_WIDTH = theme.INDICATOR + 4 * theme.CELL_INSET_H
#: What an empty cell shows - the services page's dash.
EMPTY = "—"
#: The widest a text column is fitted to; the full value is on hover.
COLUMN_CAP = 320
#: The services page's words for a table emptied by its search.
NO_MATCH = "Nothing here matches the search."
#: The least the table is given: its header and two rows. Enough to scroll
#: through, and small enough that the window still fits a 600px screen.
MIN_TABLE_HEIGHT = 88
#: The least the detail panel is given, for the same reason.
MIN_DETAIL_HEIGHT = 72


def text_inset(view):
    """Where a cell's text starts inside its column: the padding theme.py gives
    every item, plus the margin the style keeps inside that. The header is
    given the same, so each title sits over its values rather than centred
    above them - the variables table's rule (pages/cycledialogs.py)."""
    return theme.CELL_INSET_H + view.style().pixelMetric(
        QStyle.PM_FocusFrameHMargin, None, view) + 1


def indicator_rect(cell):
    """The box's square, centred in ``cell``. One function for the header and
    the rows, so the two centres cannot drift apart."""
    side = theme.INDICATOR
    box = QRectF(0, 0, side, side)
    box.moveCenter(QRectF(cell).center())
    return QRect(round(box.left()), round(box.top()), side, side)


class CheckDelegate(QStyledItemDelegate):
    """Paints the tick column: the row's band, and the box or dot centred on it."""

    def __init__(self, owner):
        super().__init__(owner)
        self._owner = owner

    def paint(self, painter, option, index):
        if index.column() != 0:
            super().paint(painter, option, index)
            return
        view = QStyleOptionViewItem(option)
        self.initStyleOption(view, index)
        # The row's own band (selection, hover) and nothing of the item's own.
        view.text = ""
        view.features &= ~QStyleOptionViewItem.HasCheckIndicator
        widget = option.widget
        style = widget.style() if widget is not None else None
        if style is not None:
            style.drawControl(QStyle.CE_ItemViewItem, view, painter, widget)
        state = QStyle.State_Enabled | (option.state & QStyle.State_MouseOver)
        state |= QStyle.State_On if self._owner.is_checked(index.row()) else QStyle.State_Off
        paint = theme.paint_dot if self._owner.mode == SINGLE else theme.paint_box
        paint(painter, indicator_rect(option.rect), state)

    def editorEvent(self, event, model, option, index):
        # A click anywhere in the tick cell is a tick, not only on the box: the
        # cell is the target, the box is what it shows.
        if index.column() == 0 and event.type() == QEvent.MouseButtonRelease \
                and event.button() == Qt.LeftButton:
            self._owner.toggle(index.row())
            return True
        return super().editorEvent(event, model, option, index)


class CheckHeader(QHeaderView):
    """The header, with an "all" box over the tick column in multiple mode.

    Tri-state like any such box: ticked when every shown row is, a bar when
    some are, empty when none are. Clicking it ticks every shown row, or
    clears them when they all are - the rows a search hides are left as they
    were, since a person cannot see what they would be changing.
    """

    def __init__(self, owner):
        super().__init__(Qt.Horizontal, owner.table)
        self._owner = owner
        self.setSectionsClickable(True)
        self.setHighlightSections(False)

    def paintSection(self, painter, rect, logical):
        painter.save()
        super().paintSection(painter, rect, logical)
        painter.restore()
        if logical != 0 or self._owner.mode != MULTIPLE:
            return
        state = QStyle.State_Enabled | {
            Qt.Checked: QStyle.State_On, Qt.PartiallyChecked: QStyle.State_NoChange,
        }.get(self._owner.all_state(), QStyle.State_Off)
        theme.paint_box(painter, indicator_rect(rect), state)

    def mousePressEvent(self, event):
        if self.logicalIndexAt(event.position().toPoint()) == 0:
            if self._owner.mode == MULTIPLE:
                self._owner.toggle_all()
            return
        super().mousePressEvent(event)


class ChoiceTable(QWidget):
    """The rows of one ``choice`` question, ticked, searched and read."""

    #: Something was ticked or unticked.
    changed = Signal()

    def __init__(self, choice, parent=None):
        super().__init__(parent)
        choice = dict(choice or {})
        self.mode = SINGLE if choice.get("mode") == SINGLE else MULTIPLE
        self.columns = [str(one) for one in choice.get("columns") or ["value"]]
        self.rows = list(choice.get("rows") or [])
        self.value_column = choice.get("value_column", -1)
        self._checked = [bool(row.get("checked")) for row in self.rows]
        if self.mode == SINGLE and sum(self._checked) > 1:
            first = self._checked.index(True)
            self._checked = [index == first for index in range(len(self.rows))]

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        self.count = widgets.mono("")
        column.addWidget(widgets.row(None, self.count))

        self.table = widgets.Table(0, 1 + len(self.columns), reclick_clears=False)
        self.header = CheckHeader(self)
        self.table.setHorizontalHeader(self.header)
        self.table.setHorizontalHeaderLabels([""] + [_heading(one) for one in self.columns])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(False)          # the list's order is the answer's
        self.table.setProperty("rows", "quiet")      # rows carry a control
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        # Room for the header and a few rows, and no more: the window has to fit
        # a small screen, and everything past this scrolls inside the table.
        self.table.setMinimumHeight(MIN_TABLE_HEIGHT)
        self.table.setItemDelegate(CheckDelegate(self))
        self.table.setMouseTracking(True)
        self._fill()
        self._note = widgets.empty_note(self.table, "")
        self.search = None
        if len(self.rows) > SEARCH_FROM:
            self.search = widgets.SearchRow(self.table, skip=(0,))
            self.search.changed.connect(self._filtered)
        self._fit()

        panel = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        panel.layout().addWidget(self.table)
        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(panel)
        self.splitter.addWidget(self._detail_panel())
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        column.addWidget(self.splitter, 1)

        self.table.itemSelectionChanged.connect(self._selected)
        self.table.cellClicked.connect(self._clicked)
        self.table.installEventFilter(self)
        first = self._checked.index(True) if True in self._checked else -1
        if first >= 0:
            self.table.selectRow(first)
        self._selected()
        self._update_count()

    # -- filling --------------------------------------------------------------
    def _fill(self):
        self.table.setRowCount(len(self.rows))
        muted = theme.color(theme.NEUTRAL[500])
        for row, data in enumerate(self.rows):
            tick = QTableWidgetItem("")
            tick.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            tick.setData(Qt.CheckStateRole,
                         Qt.Checked if self._checked[row] else Qt.Unchecked)
            self.table.setItem(row, 0, tick)
            cells = list(data.get("cells") or [])
            for index in range(len(self.columns)):
                text = str(cells[index]) if index < len(cells) else ""
                item = QTableWidgetItem(text or EMPTY)
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                if text:
                    item.setToolTip(text)
                else:
                    item.setForeground(muted)
                if index == self.value_column:
                    item.setFont(theme.mono_font())
                self.table.setItem(row, index + 1, item)

    def _fit(self):
        """Widths to the content, under a ceiling, with one column taking the rest.

        The services page's rule. The column that stretches is the one with the
        most to say on average - the title, in a list of tasks - so the short
        columns keep their width and the long one gives way to the window.
        """
        header = self.header
        header.setMinimumSectionSize(CHECK_WIDTH)
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        # Only the left padding: the band, the font and the rule under it all
        # still come from theme.py.
        header.setStyleSheet("QHeaderView::section { padding-left: %dpx; }"
                             % text_inset(self.table))
        self.table.resizeColumnsToContents()
        for index in range(1, self.table.columnCount()):
            self.table.setColumnWidth(index, min(self.table.columnWidth(index), COLUMN_CAP))
            header.setSectionResizeMode(index, QHeaderView.Interactive)
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.setColumnWidth(0, CHECK_WIDTH)
        header.setSectionResizeMode(1 + self._widest(), QHeaderView.Stretch)

    def _widest(self):
        if not self.rows:
            return len(self.columns) - 1
        totals = [0] * len(self.columns)
        for data in self.rows:
            cells = list(data.get("cells") or [])
            for index in range(len(self.columns)):
                totals[index] += len(str(cells[index])) if index < len(cells) else 0
        return totals.index(max(totals))

    def _detail_panel(self):
        panel = widgets.BlueprintPanel()
        self.detail_stack = QStackedWidget()
        self.empty_state = widgets.empty_zone("Nothing selected",
                                              "Pick a row above to see its details.")
        empty = QWidget()
        empty_column = QVBoxLayout(empty)
        empty_column.setContentsMargins(0, 0, 0, 0)
        empty_column.addWidget(self.empty_state)
        empty_column.addStretch(1)
        self.detail_stack.addWidget(empty)

        shown = QWidget()
        shown_column = QVBoxLayout(shown)
        shown_column.setContentsMargins(0, 0, 0, 0)
        shown_column.setSpacing(6)
        self.detail_title = widgets.elided_line("", "h2")
        shown_column.addWidget(self.detail_title)
        self.detail_text = QPlainTextEdit()
        self.detail_text.setReadOnly(True)
        # The same box whether or not there is anything to read: an item with
        # no detail says so in it, greyed, rather than leaving the panel to
        # look as though it failed to fill.
        self.detail_text.setPlaceholderText("No details for this item.")
        shown_column.addWidget(self.detail_text, 1)
        self.detail_stack.addWidget(shown)
        panel.layout().addWidget(self.detail_stack)
        # Said outright rather than left to the empty state's generous height:
        # on a small screen this panel gives way, so the window still fits and
        # the table and the buttons stay on it.
        panel.setMinimumHeight(MIN_DETAIL_HEIGHT)
        return panel

    # -- ticking --------------------------------------------------------------
    def is_checked(self, row):
        return 0 <= row < len(self._checked) and self._checked[row]

    def set_checked(self, row, checked=True):
        if not 0 <= row < len(self._checked):
            return
        if self.mode == SINGLE and checked:
            # One at a time: the dot moves, it is not added.
            self._checked = [index == row for index in range(len(self._checked))]
        else:
            self._checked[row] = bool(checked)
        self._sync_items()

    def toggle(self, row):
        if self.mode == SINGLE:
            self.set_checked(row, True)
        else:
            self.set_checked(row, not self.is_checked(row))

    def toggle_all(self):
        shown = self._shown()
        tick = not shown or not all(self._checked[row] for row in shown)
        for row in shown:
            self._checked[row] = tick
        self._sync_items()

    def all_state(self):
        shown = self._shown()
        ticked = sum(1 for row in shown if self._checked[row])
        if not shown or not ticked:
            return Qt.Unchecked
        return Qt.Checked if ticked == len(shown) else Qt.PartiallyChecked

    def chosen(self):
        """The ticked rows' indexes, in the list's order."""
        return [row for row, ticked in enumerate(self._checked) if ticked]

    def valid(self):
        count = len(self.chosen())
        return count == 1 if self.mode == SINGLE else count >= 1

    def _sync_items(self):
        for row, ticked in enumerate(self._checked):
            item = self.table.item(row, 0)
            if item is not None:
                item.setData(Qt.CheckStateRole, Qt.Checked if ticked else Qt.Unchecked)
        self.table.viewport().update()
        self.header.viewport().update()
        self._update_count()
        self.changed.emit()

    def _shown(self):
        return [row for row in range(self.table.rowCount())
                if not self.table.isRowHidden(row)]

    # -- what the person did --------------------------------------------------
    def _clicked(self, row, column):
        # In single mode the row is the target, as a radio label is: clicking
        # anywhere on it picks it. In multiple mode only the tick column ticks,
        # so reading a row does not change what is chosen.
        if self.mode == SINGLE and column != 0:
            self.set_checked(row, True)

    def eventFilter(self, watched, event):
        if watched is self.table and event.type() == QEvent.KeyPress \
                and event.key() == Qt.Key_Space:
            row = self.table.currentRow()
            if row >= 0:
                self.toggle(row)
            return True
        return False

    def _selected(self):
        rows = self.table.selectionModel().selectedRows()
        row = rows[0].row() if rows else -1
        if row < 0 or self.table.isRowHidden(row):
            self.detail_stack.setCurrentIndex(0)
            return
        data = self.rows[row]
        cells = list(data.get("cells") or [])
        name = (cells[self.value_column] if 0 <= self.value_column < len(cells)
                else next((one for one in cells if one), ""))
        self.detail_title.setText(str(name or EMPTY))
        self.detail_text.setPlainText(str(data.get("detail") or ""))
        self.detail_stack.setCurrentIndex(1)

    def _filtered(self):
        searching = self.search is not None and self.search.searching()
        self._note.say_filtered(NO_MATCH if searching else "")
        self._selected()
        self._update_count()
        self.header.viewport().update()

    def _update_count(self):
        chosen = self.chosen()
        if self.mode == SINGLE:
            text = ("%s chosen" % self._name(chosen[0])) if chosen else "nothing chosen yet"
        else:
            text = "%d of %d chosen" % (len(chosen), len(self.rows))
        hidden = len(self.rows) - len(self._shown())
        if hidden:
            text += ", %d shown" % (len(self.rows) - hidden)
        self.count.setText(text)

    def _name(self, row):
        cells = list(self.rows[row].get("cells") or [])
        if 0 <= self.value_column < len(cells):
            return cells[self.value_column]
        return "row %d" % (row + 1)


def _heading(name):
    """A field name as a column heading: ``due_date`` reads "due date"."""
    return str(name).replace("_", " ")
