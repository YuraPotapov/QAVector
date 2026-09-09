"""Services & Logs: every project on this machine, what is up, and what it writes.

This page is the old Log sources page with the other half of its subject added.
That page could say which backend logs a run may stream; it could not say whether
the backend was even running, because nothing in the application had ever started
one. A local app server, its database container and its log file are one thing to
person using them and were three unrelated facts here.

So the page is organised by **project** rather than by kind. Each project is a
block that folds, holding two tables:

* **Services (runners)** - things this application starts and stops. Live status,
  and a console for what they print. Owned by :mod:`~cms_gui.services`.
* **Logs (observers)** - the existing ``logsources.json`` rows, unchanged in every
  respect except that a log may now name the project it belongs to. They are
  deliberately *not* tied to the runners: a log on a staging box has no runner and
  never will, and a runner's console is not a parsed log.

Two files back this, and they stay two files. ``logsources.json`` is the
launcher's and is kept exactly as valid as the CLI expects; ``services.json`` is
the GUI's own and the launcher has never heard of it. Each is loaded, fingerprinted
and saved on its own terms, so a problem in one cannot cost the other.

The projects come first and connections last. A connection serves logs across
every project, so it cannot live inside one - but it is also the part nobody
opens twice, and putting it above the subject of the page pushed the subject
down the screen.

Three things about the *view* rather than about either file:

* **What is folded stays folded.** Every section here - a project, the unassigned
  block, the connections - is remembered in QSettings and comes back the way it
  was left, whether or not anything was saved in between. It is deliberately not
  kept in the documents: folding a block is not an edit to anybody's
  configuration, so it must neither light up Save nor need one.
* **Newest first.** Blocks, services, logs and connections are shown in the order
  they were added, most recent at the top. The rows themselves carry the date
  (``added``); the lists keep the file's order, so what starts before what is
  untouched by how the page happens to sort.
* **Every table can be searched by column**, in a row of boxes under its header.
  What is matched is what the table *says*, cell by cell, so the computed columns
  - a service's status, a log's format - are searchable with no rule of their own.

And a selection means something: picking rows in a project's services narrows its
Start, Stop and Restart to those, and the buttons say so.
"""

import json
import os

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox,
                               QComboBox, QDialog, QHeaderView, QLabel,
                               QLineEdit, QMenu, QMessageBox, QPlainTextEdit,
                               QPushButton, QScrollArea, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from .. import icons
from .. import logsourcesfile as lsf
from .. import criteria as criteria_mod
from .. import runnertypes
from .. import services as svc
from .. import servicesfile as sf
from .. import theme, widgets
from ..settings import Settings
from .logsources import ConnectionDialog, LogDialog, LogViewerDialog, RowDialog

#: What each status is called, and which of the theme's colours says it. Looked up
#: through a function rather than a module-level dict because ``theme`` rewrites
#: its colours in place when dark mode is set - a table built at import time would
#: keep painting the light palette forever.
STATUS_TEXT = {
    runnertypes.RUNNING: "Running",
    runnertypes.WAITING: "Waiting…",
    runnertypes.STARTING: "Starting…",
    runnertypes.STOPPING: "Stopping…",
    runnertypes.STOPPED: "Stopped",
    runnertypes.FAILED: "Failed",
}


def status_color(status):
    return {runnertypes.RUNNING: theme.OK,
            runnertypes.WAITING: theme.NEUTRAL[500],
            runnertypes.STARTING: theme.WARN,
            runnertypes.STOPPING: theme.WARN,
            runnertypes.FAILED: theme.BAD}.get(status, theme.NEUTRAL[500])


class Table(QTableWidget):
    """A table whose selected row can be un-selected, and which can carry a
    search row under its header.

    **Selection.** Clicking a selected row clears it. Selection is what aims Open
    Tail and Open Full at a log, and what narrows a project's Start and Stop to
    part of it, so there has to be a way to aim at nothing. With one row in the
    list there was none: the first click selected it and every click after that
    re-selected the same thing, and a list of one could never be returned to
    having nothing chosen. Where the table selects several rows at a time, Qt
    already toggles on click, so this stands aside.

    **The search row** is a child of the *view* rather than of the page, parked
    in the margin between the header and the first row: that way it stays put
    while the rows change under it, and it is measured in the table's own
    coordinates, which is what lets a box line up with the column it searches.
    That margin belongs to QTableView - it recomputes it on every geometry change
    to make room for the header - so the only place to claim a strip of it is
    here, straight after the base class has had its say.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._search_row = None
        # updateGeometries runs again from inside setViewportMargins, and the
        # nested call would hand the margins back to the header alone - which is
        # exactly the strip we are claiming.
        self._laying_out = False

    def set_search_row(self, widget):
        self._search_row = widget
        widget.setParent(self)
        widget.show()
        self.updateGeometries()

    def top_margin(self):
        """Header plus search row: everything above the first row of data."""
        return self.viewportMargins().top()

    def updateGeometries(self):
        if self._laying_out:
            return
        self._laying_out = True
        try:
            super().updateGeometries()
            row = self._search_row
            if row is None:
                return
            margins = self.viewportMargins()
            height = row.sizeHint().height()
            self.setViewportMargins(margins.left(), margins.top() + height,
                                    margins.right(), margins.bottom())
            row.setGeometry(margins.left() + self.frameWidth(),
                            margins.top() + self.frameWidth(),
                            self.viewport().width(), height)
        finally:
            self._laying_out = False

    def mousePressEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        if (self.selectionMode() == QAbstractItemView.SingleSelection
                and index.isValid()
                and index.row() in {i.row() for i in self.selectedIndexes()}):
            self.clearSelection()
            self.setCurrentIndex(self.model().index(-1, -1))
            return
        super().mousePressEvent(event)


class SearchRow(QWidget):
    """One search box per column, under the table's header.

    A box per column rather than one over the whole row, because the columns ask
    different questions: "postgres" in Name finds a service, in Config it finds
    every service that mentions the container, and in Status it finds nothing at
    all. Typing in more than one narrows - they are joined with AND, which is what
    a row of boxes looks like it does.

    What is searched is what the table *says*, cell by cell, not the rows behind
    it. So a search matches exactly what a person can read on screen, including
    the columns that are computed rather than stored - a service's status, a log's
    format - and no column needs a rule of its own here.
    """

    changed = Signal()

    #: The strip's height, and the box's inside it. Sized from the row rather
    #: than from a form field: this stands between the header and the first row
    #: and has to read as part of the table's own furniture.
    HEIGHT = 26
    INSET = 3

    def __init__(self, table, skip=(), parent=None):
        super().__init__(parent)
        self.setProperty("role", "searchrow")
        # A plain QWidget paints no background from a stylesheet without this,
        # so the strip would show the page through it.
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(self.HEIGHT)
        self._table = table
        self._edits = {}
        for column in range(table.columnCount()):
            if column in set(skip):
                continue
            edit = QLineEdit(self)
            heading = table.horizontalHeaderItem(column)
            edit.setPlaceholderText((heading.text() if heading else "").lower()
                                    or "search")
            edit.setProperty("role", "search")
            edit.textChanged.connect(self.apply)
            self._edits[column] = edit
        table.set_search_row(self)
        header = table.horizontalHeader()
        header.sectionResized.connect(self._place)
        table.horizontalScrollBar().valueChanged.connect(self._place)

    def sizeHint(self):
        return QSize(self._table.viewport().width(), self.HEIGHT)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place()

    def _place(self, *_args):
        """Every box over the column it searches, at whatever width that is now.

        Flush with the column's own edges rather than inset inside them. Inset,
        the first box sat against the table's border while every other one had a
        gap on each side, so a row of boxes meant to line up with the columns
        read as a row of unevenly spaced ones. Sharing edges also means the boxes
        divide the strip exactly where the header divides its sections.
        """
        header = self._table.horizontalHeader()
        for column, edit in self._edits.items():
            if self._table.isColumnHidden(column):
                edit.hide()
                continue
            edit.show()
            # One pixel wider than the column, so this box's right border lands
            # under the next box's left one and the two read as a single rule
            # rather than as a double.
            edit.setGeometry(header.sectionViewportPosition(column), self.INSET,
                             max(header.sectionSize(column) + 1, 0),
                             self.HEIGHT - 2 * self.INSET)

    def box(self, column):
        """The search box over one column, or None where there is not one."""
        return self._edits.get(column)

    def needles(self):
        """{column: what to look for}, lower-cased, empty boxes left out."""
        return {column: edit.text().strip().lower()
                for column, edit in self._edits.items() if edit.text().strip()}

    def searching(self):
        return bool(self.needles())

    def clear(self):
        for edit in self._edits.values():
            edit.clear()

    def state(self):
        """What is typed here, keyed by column, for putting back after a rebuild."""
        return {str(column): edit.text() for column, edit in self._edits.items()
                if edit.text()}

    def set_state(self, state):
        for column, edit in self._edits.items():
            edit.setText((state or {}).get(str(column), ""))

    def apply(self, *_args):
        """Hide what does not match. Called again whenever the rows are rebuilt."""
        needles = self.needles()
        table = self._table
        for row in range(table.rowCount()):
            table.setRowHidden(row, not self._matches(row, needles))
        self._place()
        self.changed.emit()

    def _matches(self, row, needles):
        for column, needle in needles.items():
            item = self._table.item(row, column)
            if needle not in (item.text().lower() if item is not None else ""):
                return False
        return True


def _stamped(row):
    """Mark a row as added now, unless it already says when it was.

    Only rows *created* here are stamped. An edited row keeps the date it had -
    changing a service's port does not make it a new service - and a copy is
    stamped afresh, because that is one more row than there was.
    """
    if not row.added:
        row.added = lsf.stamp()
    return row


def _newest_first(rows):
    """The order the page shows rows in: most recently added at the top.

    A *view* order and nothing more. The lists themselves keep the file's order,
    which is what :meth:`servicesfile.ProjectRow.start_order` reads - so putting a
    new service at the top of a block does not make it start before the database
    it was added after.

    Rows written before the editor stamped them carry no date. They keep the
    file's order, below the ones that do: guessing that an undated row is old
    would be a guess, but showing it under everything that says when it arrived
    is only saying what is known.
    """
    dated = [row for row in rows if getattr(row, "added", "")]
    undated = [row for row in rows if not getattr(row, "added", "")]
    # reverse=True on a stable sort keeps rows sharing a second in file order.
    return sorted(dated, key=lambda row: row.added, reverse=True) + undated


def _table(headers, actions_col, stretch):
    """The page's one table recipe - the same one the log tables have always used."""
    table = Table(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    # Read-only: every edit goes through a dialog, so a cell that looks editable
    # and is not would be worse than one that plainly is not.
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    # The page scrolls, the tables do not: a scrollbar inside a block inside a
    # scrolling page is two ways to move the same rows and neither reaches the end.
    table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    # Every row here carries buttons, so selection is a quiet band rather than
    # the accent flood - see the rule in theme.STYLESHEET.
    table.setProperty("rows", "quiet")
    header = table.horizontalHeader()
    header.setSectionResizeMode(stretch, QHeaderView.Stretch)
    header.setSectionResizeMode(actions_col, QHeaderView.Fixed)
    return table


def _fit_columns(table, actions_col, cap=320):
    """Size to content, but never let one long value squeeze out the rest.

    A name or a path can be arbitrarily long, and sizing purely to content hands
    it the whole width - leaving the column that says what the row actually is
    truncated to an ellipsis. The full value is still there on hover.
    """
    table.resizeColumnsToContents()
    for index in range(table.columnCount()):
        if index != actions_col:
            table.setColumnWidth(index, min(table.columnWidth(index), cap))
    _fit_actions(table, actions_col)
    _fit_rows(table, actions_col)
    _fit_height(table)


def _fit_actions(table, column):
    """Give a column of widgets the width they actually take, not a number.

    Written down, that number is wrong the first time the body font changes -
    which is how "Console" and "Delete" came out as "onsol" and "Delet". And
    resizeColumnsToContents does not help: it measures *items*, and a cell
    holding a widget has none. The strip knows its own width; ask it.
    """
    widest = 0
    for row in range(table.rowCount()):
        strip = table.cellWidget(row, column)
        if strip is None:
            continue
        strip.ensurePolished()
        widest = max(widest, strip.sizeHint().width())
    if not widest:
        return
    # Never narrower than its own title. The criteria column shows only what has
    # matched, so with nothing lit the widgets ask for almost nothing - and the
    # column sized to that clipped its heading to "RITERI".
    header = table.horizontalHeader().sectionSizeHint(column)
    table.setColumnWidth(column, max(widest + 2 * theme.CELL_INSET_H, header))


def _fit_rows(table, actions_col):
    """Make every row tall enough for the buttons it holds.

    The same measurement the Environments page makes for its inline editor: the
    view insets a cell widget by the item padding on both sides, so a row sized
    for text hands a 30px button less than 30px and the difference hangs across
    the gridline below.
    """
    tallest = 0
    for row in range(table.rowCount()):
        strip = table.cellWidget(row, actions_col)
        if strip is None:
            continue
        strip.ensurePolished()
        tallest = max(tallest, strip.minimumSizeHint().height())
    if tallest:
        table.verticalHeader().setDefaultSectionSize(
            tallest + 2 * theme.CELL_INSET_V)


def _fit_height(table):
    """Give the table exactly the height its rows need.

    Inside a scrolling page a table has no natural height - it takes whatever the
    layout offers, which is either nothing or everything. Measuring the rows is
    what makes a block's height mean "how much is in it".

    Rows hidden by a search do not count. A table that kept its full height while
    showing one match would leave the band of nothing the search was meant to
    clear away, and a page of them would not look searched at all.
    """
    row_height = table.verticalHeader().defaultSectionSize()
    shown = sum(1 for row in range(table.rowCount())
                if not table.isRowHidden(row))
    # Everything above the first row of data, which is the header *and* the
    # search row standing under it - measuring the header alone would clip the
    # last row by exactly the strip's height.
    height = table.top_margin() + 4
    height += row_height * shown if shown else 30
    table.setFixedHeight(height)


def _fit_list(view, rows, min_rows=1, max_rows=6):
    """Give a list or grid the height its entries need, and no more.

    A fixed maximum is not a height: an empty grid and a one-item list both took
    the whole of it and left a field that is mostly nothing. Measured from the
    rows, floored so there is somewhere for "+ Add" to land, and capped so a long
    list scrolls rather than pushing the buttons off the bottom of the dialog.
    """
    # What the view will actually draw, which is not what sizeHintForRow says: a
    # cell whose text wraps reports the wrapped height, so a criteria row holding
    # a long rule summary claimed 71px for a row drawn at 30 and reserved two
    # rows' worth of nothing under the table.
    row = 0
    if rows:
        height_of = getattr(view, "rowHeight", None)
        row = height_of(0) if height_of is not None else view.sizeHintForRow(0)
    if row <= 0:
        # Empty, so there is no row to measure - but a table still knows how tall
        # one will be, and agreeing with that is what keeps the grid from shifting
        # the moment its first row lands.
        header = getattr(view, "verticalHeader", None)
        row = header().defaultSectionSize() if header is not None else 0
    if row <= 0:
        row = view.fontMetrics().height() + 8
    shown = max(min_rows, min(rows, max_rows))
    header = getattr(view, "horizontalHeader", None)
    # sizeHint, not height, and isHidden rather than isVisible: this runs while
    # the dialog is still being built, and a header that has never been shown
    # reports neither a height nor visibility - which took its own row's worth of
    # space off the grid and left the column titles sitting on nothing.
    extra = (header().sizeHint().height()
             if header is not None and not header().isHidden() else 0)
    view.setFixedHeight(shown * row + extra + 2 * view.frameWidth() + 2)


def _status_width(table):
    """Room for the longest thing the status column will ever say, plus its dot."""
    metrics = table.fontMetrics()
    widest = max(metrics.horizontalAdvance("%s (edited)" % text)
                 for text in STATUS_TEXT.values())
    return widest + 44


class CriterionTag(QLabel):
    """One criterion beside its service: its name, lit or not.

    Its own class rather than widgets.Tag because that one builds its colours
    from a dict evaluated at import, which freezes the light palette; these are
    read from the theme every time the tag is set, so dark mode follows.
    """

    def __init__(self, parent=None):
        super().__init__("", parent)
        self.setAlignment(Qt.AlignCenter)

    def set_state(self, name, color, lit, outstanding=()):
        ink = criteria_mod.color_of(color) if lit else criteria_mod.unlit_color()
        self.setText(name or "(unnamed)")
        self.setStyleSheet(
            "color: %s; border: 1px solid %s; background: transparent;"
            "font-family: %s; font-size: 10px; letter-spacing: 0.5px;"
            "padding: 1px 6px;" % (ink, ink, theme.MONO_CSS))
        if lit:
            self.setToolTip("%s: matched" % (name or "criterion"))
        else:
            # What is keeping it dark is the useful half - "not yet" on its own
            # says nothing about whether it ever will be.
            self.setToolTip("%s: %s" % (name or "criterion",
                                        "; ".join(outstanding) or "not matched yet"))


def _criteria_strip(criteria):
    """The cell that shows a service's criteria - the ones that have matched.

    Only those. A row carrying every criterion whether or not it had fired was
    mostly grey words about things that had not happened, which is the noise this
    column was added to cut through. What is being watched for is a question with
    an answer, so it goes on the tooltip rather than into the row.
    """
    tags = []
    for criterion in criteria:
        tag = CriterionTag()
        tag.set_state(criterion.name, criterion.color, False)
        tag.setVisible(False)
        tags.append(tag)
    strip = widgets.row(*tags, None, spacing=4) if tags else QWidget()
    # Transparent, so the row's selection band shows through it - the same
    # reason the action strips are.
    widgets.scoped_style(strip, "background: transparent;")
    strip.tags = tags
    strip.setToolTip(_watching_for([(c.name, "", False, []) for c in criteria]))
    return strip


def _watching_for(state):
    """What the cell says on hover: what has matched, and what has not yet."""
    lit = [name for name, _colour, is_lit, _why in state if is_lit]
    dark = [name for name, _colour, is_lit, _why in state if not is_lit]
    lines = []
    if lit:
        lines.append("Matched: %s" % ", ".join(lit))
    if dark:
        lines.append("Still watching for: %s" % ", ".join(dark))
    return "\n".join(lines)


def _cell(table, row, column, text, mono=False, tip=None):
    item = QTableWidgetItem(text)
    if mono:
        item.setFont(theme.mono_font(9))
    item.setToolTip(tip if tip is not None else text)
    table.setItem(row, column, item)
    return item


def _ghost(label, handler, tip="", variant="ghost"):
    button = QPushButton(label)
    button.setProperty("variant", variant)
    if tip:
        button.setToolTip(tip)
    button.clicked.connect(lambda _checked=False: handler())
    return button


def _cell_button(label, handler, tip=""):
    """A row's own button: neutral ink, so it reads on a selected row too."""
    return _ghost(label, handler, tip, variant="cell")


def _actions(*buttons):
    """The strip of buttons that goes in a row's last cell.

    Transparent, so the row's own selection band shows through it. A widget put
    in a cell paints the global QWidget background otherwise, which is how the
    buttons ended up stranded on a rectangle of the wrong colour whenever their
    row was selected.
    """
    strip = widgets.row(*buttons, None, spacing=2)
    widgets.scoped_style(strip, "background: transparent;")
    return strip


def _icon_button(name, handler, tip):
    button = QPushButton("")
    button.setProperty("variant", "cell")
    button.setToolTip(tip)
    button.setFixedWidth(30)
    icons.button(button, name, "")
    button.clicked.connect(lambda _checked=False: handler())
    return button


# ----------------------------------------------------------------- environment
class EnvEditor(QWidget):
    """A name/value grid for a runner's extra environment variables."""

    changed = Signal()

    def __init__(self, values=None, parent=None):
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Name", "Value"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.table.itemChanged.connect(lambda _item: self.changed.emit())
        widgets.empty_note(self.table,
                           "Nothing added - the service inherits this "
                           "application's own environment.")
        column.addWidget(self.table)
        column.addWidget(widgets.row(_ghost("+ Add", self.add_row),
                                     _ghost("Remove", self.remove_row), None))
        self.set_values(values or {})

    def add_row(self, name="", value=""):
        index = self.table.rowCount()
        self.table.insertRow(index)
        self.table.setItem(index, 0, QTableWidgetItem(name))
        self.table.setItem(index, 1, QTableWidgetItem(value))
        self._fit()
        self.changed.emit()

    def remove_row(self):
        index = self.table.currentRow()
        if index >= 0:
            self.table.removeRow(index)
            self._fit()
            self.changed.emit()

    def set_values(self, values):
        self.table.setRowCount(0)
        for name in sorted(values):
            self.add_row(name, str(values[name]))
        self._fit()

    def _fit(self):
        # One empty row's worth when there is nothing: enough to read as a grid
        # waiting to be filled, rather than a header with a void under it.
        _fit_list(self.table, self.table.rowCount(), min_rows=1, max_rows=5)
        self.setFixedHeight(self.sizeHint().height())

    def values(self):
        result = {}
        for index in range(self.table.rowCount()):
            name = self.table.item(index, 0)
            value = self.table.item(index, 1)
            key = (name.text() if name else "").strip()
            if key:
                result[key] = (value.text() if value else "").strip()
        return result


# --------------------------------------------------------------------- dialogs
class ProjectDialog(RowDialog):
    """Create or edit one project: what it is called and where it lives."""

    def __init__(self, row=None, taken=(), parent=None):
        super().__init__("Project" if row else "New project", parent)
        self._row = (row or sf.ProjectRow()).copy()
        self._taken = {name for name in taken if name != self._row.name}

        self.name = QLineEdit(self._row.name)
        self.name.setPlaceholderText("Storefront, Billing…")
        self.name.textChanged.connect(self._changed)
        self.column.addWidget(widgets.field(
            "Name", self.name, "What the block is called, and what a log names "
                               "to appear under it."))

        self.dir = QLineEdit(self._row.dir)
        self.dir.setPlaceholderText("~/PycharmProjects/MyProject")
        self.dir.textChanged.connect(self._changed)
        browse = _ghost("Browse…", self._pick_dir)
        self.column.addWidget(widgets.field(
            "Directory", widgets.row(self.dir, browse),
            "Where the project lives. Every service starts here unless it names a "
            "directory of its own. It does not have to exist yet."))

        self.finish_layout()
        self._changed()

    def _pick_dir(self):
        path = widgets.pick_path(self, "Project directory",
                                 self.dir.text().strip() or "~", directory=True)
        if path:
            self.dir.setText(path)

    def _changed(self, *_args):
        self.show_problems(self._problems())

    def _problems(self):
        row = self.value()
        problems = []
        if not row.name.strip():
            problems.append("A name is required.")
        elif row.name in self._taken:
            problems.append("There is already a project called %r." % row.name)
        directory = row.dir.strip()
        if directory and os.path.isfile(os.path.expanduser(directory)):
            problems.append("%s is a file, not a directory." % directory)
        return problems

    def value(self):
        return sf.ProjectRow(name=self.name.text().strip(),
                             dir=self.dir.text().strip(),
                             expanded=self._row.expanded,
                             runners=[r.copy() for r in self._row.runners],
                             extra=dict(self._row.extra),
                             added=self._row.added)


class RunnerDialog(RowDialog):
    """Create or edit one service. The form is generated from its type.

    Four hand-written editors would be four near-identical forms that drift
    apart, and what actually distinguishes a Python service from a container is
    its command lines, not its layout. So each type declares its fields and this
    builds them - which also means a type added later needs no dialog at all.
    """

    def __init__(self, runner_type, row=None, taken=(), siblings=(),
                 project_dir="", parent=None):
        super().__init__(runner_type.label if row else "New %s"
                         % runner_type.label.lower(), parent)
        self._type = runner_type
        # Where Browse opens, and where a virtualenv is looked for.
        self._project_dir = project_dir
        self._row = (row or sf.RunnerRow(type=runner_type.id,
                                         settings=runner_type.default_settings())).copy()
        self._taken = {name for name in taken if name != self._row.name}
        self._editors = {}

        self.name = QLineEdit(self._row.name)
        self.name.setPlaceholderText("App Server, PostgreSQL DB…")
        self.name.textChanged.connect(self._changed)
        self.column.addWidget(widgets.field(
            "Name", self.name, "Unique within this project."))

        for spec in self._type.form_fields():
            self.column.addWidget(self._build(spec))

        # Every other service in this project, so an order is picked rather than
        # typed. A name that is not on the list is a name that cannot be waited
        # for, and the checklist is the only place that knows which those are.
        self.depends = widgets.CheckList(searchable=False)
        self.depends.set_noun("services")
        others = [name for name in siblings if name and name != self._row.name]
        for name in others:
            self.depends.add(name, name, "")
        self.depends.set_checked([d for d in self._row.depends if d in others])
        _fit_list(self.depends.list, len(others), min_rows=1, max_rows=6)
        self.depends.changed.connect(self._changed)
        self.depends_field = widgets.field(
            "Starts after", self.depends,
            "Start waits until each of these reports running, and Stop takes them "
            "down in the reverse order. \"Running\" means the process is up, not "
            "that whatever is inside it has finished booting.")
        self.column.addWidget(self.depends_field)
        # Nothing to depend on is not a choice worth showing an empty box for.
        self.depends_field.setVisible(bool(others))

        self.criteria = CriteriaEditor(self._row.criteria, self._project_dir)
        self.criteria.changed.connect(self._changed)
        self.column.addWidget(widgets.field(
            "Criteria", self.criteria,
            "What its own log says about where it has got to, shown beside the "
            "service. Display only: a criterion never changes the status, never "
            "holds anything else up, and never stops anything."))

        self.detach = QCheckBox("Detach allowed - keep running when this window closes")
        self.detach.setChecked(bool(self._row.detach))
        self.detach.toggled.connect(self._changed)
        self.column.addWidget(self.detach)
        self.detach_hint = QLabel("")
        self.detach_hint.setProperty("role", "hint")
        self.detach_hint.setWordWrap(True)
        self.column.addWidget(self.detach_hint)
        if not self._type.detach_choice:
            # Not a preference for a container: nothing here started it in a way
            # that could be undone by closing a window.
            self.detach.setChecked(bool(self._type.detach_default))
            self.detach.setEnabled(False)
            self.detach_hint.setText(self._type.detach_note)
        else:
            self.detach_hint.setText(
                "Off: closing the application asks first, then stops it. On: it "
                "keeps running on its own and is picked up again next time.")

        self.finish_layout()
        self._changed()

    def _build(self, spec):
        """One field of the generated form, by kind."""
        if spec.kind == "env":
            editor = EnvEditor(self._row.settings.get(spec.key) or {})
            editor.changed.connect(self._changed)
            self._editors[spec.key] = editor
            return widgets.field(spec.label, editor, spec.hint)
        if spec.kind == "check":
            box = QCheckBox("")
            box.setChecked(bool(self._row.settings.get(spec.key)))
            box.toggled.connect(self._changed)
            self._editors[spec.key] = box
            return widgets.field(spec.label, box, spec.hint)
        edit = QLineEdit(str(self._row.settings.get(spec.key) or ""))
        edit.textChanged.connect(self._changed)
        self._editors[spec.key] = edit
        if spec.key == "interpreter":
            # Say what leaving it blank will actually run. A detected venv is the
            # answer most of the time, and it lives where a chooser cannot go.
            found = runnertypes.venv_python(self._project_dir)
            edit.setPlaceholderText(found or runnertypes.DEFAULT_PYTHON)
        if spec.kind in ("file", "dir"):
            browse = _ghost("Browse…", lambda s=spec: self._pick(s))
            return widgets.field(spec.label, widgets.row(edit, browse), spec.hint)
        return widgets.field(spec.label, edit, spec.hint)

    def _pick(self, spec):
        edit = self._editors[spec.key]
        # A chooser does not list dotted directories, but it does show what is
        # inside one it opens in - so start at the answer when we can guess it.
        start = edit.text().strip()
        if not start and spec.key == "interpreter":
            start = runnertypes.venv_python(self._project_dir)
        path = widgets.pick_path(self, spec.label, start or self._project_dir,
                                 directory=(spec.kind == "dir"))
        if path:
            edit.setText(path)

    def _changed(self, *_args):
        self.show_problems(self._problems())

    def _problems(self):
        row = self.value()
        problems = []
        if not row.name.strip():
            problems.append("A name is required.")
        elif row.name in self._taken:
            problems.append("There is already a service called %r in this project."
                            % row.name)
        problems.extend(self._type.problems(row.settings))
        seen = set()
        for index, one in enumerate(row.criteria, start=1):
            where = "Criterion %d (%s)" % (index, one.name or "unnamed")
            if one.name and one.name in seen:
                problems.append("%s: two criteria share that name." % where)
            seen.add(one.name)
            problems.extend(criteria_mod.problems(one, where))
        return problems

    def value(self):
        settings = dict(self._row.settings)
        for spec in self._type.form_fields():
            editor = self._editors[spec.key]
            if spec.kind == "env":
                filled = editor.values()
            elif spec.kind == "check":
                filled = editor.isChecked()
            else:
                filled = editor.text().strip()
            # A field this row never had, left empty, stays absent. Writing it
            # back as "" is a change to the file like any other, so a form that
            # was only opened and closed lit Save up and offered to rewrite every
            # service - which is what happened the first time a field was added
            # to a form that already had rows in the wild (Stop timeout).
            # Whatever the row already carries is written whatever it says now,
            # so clearing a field still saves as a blank.
            if filled or spec.key in settings:
                settings[spec.key] = filled
        return sf.RunnerRow(name=self.name.text().strip(), type=self._type.id,
                            detach=self.detach.isChecked(), settings=settings,
                            depends=list(self.depends.checked()),
                            criteria=self.criteria.rows(),
                            extra=dict(self._row.extra),
                            added=self._row.added)


# ----------------------------------------------------------------- criteria
class CriterionDialog(RowDialog):
    """Create or edit one criterion: what to watch for, and what to call it."""

    OWN_OUTPUT = "(this service's own output)"

    def __init__(self, row=None, taken=(), project_dir="", parent=None):
        super().__init__("Criterion" if row else "New criterion", parent)
        self._row = (row or criteria_mod.CriterionRow(
            rules=[criteria_mod.Rule()])).copy()
        self._taken = {name for name in taken if name != self._row.name}
        self._project_dir = project_dir

        self.name = QLineEdit(self._row.name)
        self.name.setPlaceholderText("start, finished_tests, stopped…")
        self.name.textChanged.connect(self._changed)
        self.column.addWidget(widgets.field(
            "Name", self.name,
            "Yours to choose - it is what the tag beside the service says."))

        self.color = QComboBox()
        for name in criteria_mod.COLORS:
            self.color.addItem(icons.icon("dot", criteria_mod.color_of(name)),
                               name)
        self.color.setCurrentText(self._row.color)
        self.color.currentTextChanged.connect(self._changed)
        self.column.addWidget(widgets.field(
            "Colour", self.color,
            "What it turns once it matches. Named rather than picked, so it "
            "follows dark mode instead of being one fixed value."))

        self.source = QLineEdit(self._row.source)
        self.source.setPlaceholderText(self.OWN_OUTPUT)
        self.source.textChanged.connect(self._changed)
        browse = _ghost("Browse…", self._pick_source)
        self.column.addWidget(widgets.field(
            "Watch", widgets.row(self.source, browse),
            "Blank reads what the service prints. Name a file for a backend that "
            "logs to one instead - a service started with a logfile prints almost "
            "nothing to its console."))

        self.rules = QTableWidget(0, 3)
        self.rules.setHorizontalHeaderLabels(["Condition", "Kind", "Pattern"])
        self.rules.verticalHeader().setVisible(False)
        self.rules.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.rules.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.rules.itemChanged.connect(lambda _item: self._changed())
        widgets.empty_note(self.rules,
                           "No rules, so this could never match anything.")
        self.column.addWidget(widgets.field(
            "Rules", self.rules,
            "All of them have to hold, and they are about the whole log rather "
            "than one line: \"must contain\" is satisfied by any line at any "
            "point, \"must not contain\" stops being satisfied the moment one "
            "matches. Case-sensitive, like grep; a regex can open with (?i)."))
        self.column.addWidget(widgets.row(
            _ghost("+ Add rule", self.add_rule),
            _ghost("Remove rule", self.remove_rule), None))
        for rule in self._row.rules or [criteria_mod.Rule()]:
            self.add_rule(rule)

        self.finish_layout()
        self._changed()

    def _pick_source(self):
        path = widgets.pick_path(self, "Log file",
                                 self.source.text().strip() or self._project_dir)
        if path:
            self.source.setText(path)

    def add_rule(self, rule=None):
        rule = rule if isinstance(rule, criteria_mod.Rule) else criteria_mod.Rule()
        index = self.rules.rowCount()
        self.rules.insertRow(index)
        mode = QComboBox()
        for value in criteria_mod.MODES:
            mode.addItem(criteria_mod.MODE_LABELS[value], value)
        mode.setCurrentIndex(list(criteria_mod.MODES).index(rule.mode))
        mode.currentIndexChanged.connect(self._changed)
        kind = QComboBox()
        for value in criteria_mod.KINDS:
            kind.addItem(criteria_mod.KIND_LABELS[value], value)
        kind.setCurrentIndex(list(criteria_mod.KINDS).index(rule.kind))
        kind.currentIndexChanged.connect(self._changed)
        self.rules.setCellWidget(index, 0, mode)
        self.rules.setCellWidget(index, 1, kind)
        self.rules.setItem(index, 2, QTableWidgetItem(rule.pattern))
        self._fit_rules()
        self._changed()

    def remove_rule(self):
        index = self.rules.currentRow()
        if index >= 0:
            self.rules.removeRow(index)
            self._fit_rules()
            self._changed()

    def _fit_rules(self):
        self.rules.resizeColumnsToContents()
        # The first two hold combo boxes, and resizeColumnsToContents measures
        # items - a cell holding a widget has none, so both came out clipped to
        # "must not contai" and "rege".
        for column in (0, 1):
            _fit_actions(self.rules, column)
        self.rules.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        # And tall enough for them. A row sized for text hands a 30px combo the
        # 24px left after the cell inset; it renders at its own 30 regardless and
        # the difference hangs across the gridline below - which is the lopsided
        # row rather than anything about the two combos differing.
        _fit_rows(self.rules, 0)
        _fit_list(self.rules, self.rules.rowCount(), min_rows=1, max_rows=6)
        self._fit()

    def _changed(self, *_args):
        self.show_problems(self._problems())

    def _problems(self):
        row = self.value()
        found = []
        if row.name and row.name in self._taken:
            found.append("There is already a criterion called %r." % row.name)
        found.extend(criteria_mod.problems(row, "This criterion"))
        return found

    def value(self):
        rules = []
        for index in range(self.rules.rowCount()):
            mode = self.rules.cellWidget(index, 0)
            kind = self.rules.cellWidget(index, 1)
            pattern = self.rules.item(index, 2)
            rules.append(criteria_mod.Rule(
                mode.currentData(), kind.currentData(),
                (pattern.text() if pattern else "").strip(),
                dict(self._row.rules[index].extra)
                if index < len(self._row.rules) else None))
        return criteria_mod.CriterionRow(
            name=self.name.text().strip(), color=self.color.currentText(),
            source=self.source.text().strip(), rules=rules,
            extra=dict(self._row.extra))


class CriteriaEditor(QWidget):
    """The criteria table inside a service's own form."""

    changed = Signal()

    def __init__(self, criteria=(), project_dir="", parent=None):
        super().__init__(parent)
        self._rows = [one.copy() for one in criteria]
        self._project_dir = project_dir
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Name", "Colour", "Watches", "Rules"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.table.doubleClicked.connect(lambda index: self.edit(index.row()))
        widgets.empty_note(self.table,
                           "Nothing watched for. Add one to have this service "
                           "say where it has got to.")
        column.addWidget(self.table)
        column.addWidget(widgets.row(_ghost("+ Add criterion", self.add),
                                     _ghost("Edit", self.edit),
                                     _ghost("Remove", self.remove), None))
        self._rebuild()

    def rows(self):
        return [one.copy() for one in self._rows]

    def add(self):
        dialog = CriterionDialog(taken=[one.name for one in self._rows],
                                 project_dir=self._project_dir, parent=self)
        if dialog.exec() == QDialog.Accepted:
            self._rows.append(dialog.value())
            self._rebuild()
            self.changed.emit()

    def edit(self, index=None):
        index = self.table.currentRow() if index is None or index is False else index
        if not (0 <= index < len(self._rows)):
            return
        dialog = CriterionDialog(self._rows[index],
                                 taken=[one.name for one in self._rows],
                                 project_dir=self._project_dir, parent=self)
        if dialog.exec() == QDialog.Accepted:
            self._rows[index] = dialog.value()
            self._rebuild()
            self.changed.emit()

    def remove(self):
        index = self.table.currentRow()
        if 0 <= index < len(self._rows):
            del self._rows[index]
            self._rebuild()
            self.changed.emit()

    def _rebuild(self):
        self.table.setRowCount(len(self._rows))
        for index, one in enumerate(self._rows):
            _cell(self.table, index, 0, one.name or "(unnamed)", mono=True)
            item = QTableWidgetItem(one.color)
            item.setIcon(icons.icon("dot", criteria_mod.color_of(one.color)))
            self.table.setItem(index, 1, item)
            _cell(self.table, index, 2, one.watches(), mono=True)
            _cell(self.table, index, 3, one.summary() or "(no rules)")
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        _fit_list(self.table, len(self._rows), min_rows=1, max_rows=5)
        self.setFixedHeight(self.sizeHint().height())


class ConsoleWindow(QDialog):
    """What one service is printing, live, and what it printed before.

    Not modal and not tied to the row it came from: watching a service while
    fixing the configuration that starts it is the ordinary thing to want.
    """

    VISIBLE_LINES = 5000

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.setWindowTitle("%s · %s" % (service.project, service.name))
        self.resize(880, 520)
        self._service = service

        column = QVBoxLayout(self)
        column.setContentsMargins(16, 14, 16, 14)
        column.setSpacing(8)
        self.status = widgets.Tag("", "neutral")
        column.addWidget(widgets.row(
            widgets.heading("%s · %s" % (service.project, service.name), "h2"),
            self.status, None,
            _ghost("Open log file", self._open_file,
                   "The whole file, in whatever opens .log here."),
            _ghost("Clear", self._clear, "Clears this view, not the file.")))
        self.path = widgets.elided_mono(service.log_path)
        column.addWidget(self.path)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.view.setMaximumBlockCount(self.VISIBLE_LINES)
        self.view.setStyleSheet("font-family: %s; font-size: 12px;" % theme.MONO_CSS)
        column.addWidget(self.view, 1)

        self.view.setPlainText("\n".join(service.console()))
        self._scroll_to_end()
        service.output.connect(self._append)
        service.status_changed.connect(self._status_changed)
        self._status_changed(service.status)

    def _append(self, line):
        self.view.appendPlainText(line)

    def _clear(self):
        self.view.clear()

    def _scroll_to_end(self):
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _status_changed(self, status):
        variant = {runnertypes.RUNNING: "accent",
                   runnertypes.STARTING: "outline",
                   runnertypes.STOPPING: "outline",
                   runnertypes.FAILED: "bad"}.get(status, "neutral")
        self.status.set(STATUS_TEXT.get(status, status).upper(), variant)

    def _open_file(self):
        if os.path.exists(self._service.log_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._service.log_path))


# ----------------------------------------------------------------------- block
class ProjectBlock(QWidget):
    """One project, folded or open: its services above its logs.

    Built on :class:`widgets.Disclosure`, which already is the accordion - a
    header line that can carry controls, and a body that folds away. The project's
    own buttons go on that header line rather than beside the whole block, so
    they read as belonging to the title and not to the tables.
    """

    RUNNER_HEADERS = ["Name", "Type", "Config", "Status", "Criteria", ""]
    (R_NAME, R_TYPE, R_CONFIG, R_STATUS, R_CRITERIA, R_ACTIONS) = range(6)

    LOG_HEADERS = ["Name", "Environments", "Reads", "Over", "Format", "Default", ""]
    (L_NAME, L_ENVS, L_READS, L_CONN, L_FORMAT, L_DEFAULT, L_ACTIONS) = range(7)

    def __init__(self, page, project, parent=None):
        super().__init__(parent)
        self.page = page
        #: The :class:`servicesfile.ProjectRow`, or None for the block that holds
        #: the logs belonging to no project.
        self.project = project
        self.name = project.name if project is not None else ""
        self._runner_rows = []
        self._log_rows = []
        self._service_buttons = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        # Filled when it is not a project: the block for logs belonging to none
        # is a part of the page, like Connections, not a row somebody added.
        panel = widgets.BlueprintPanel(padding=(14, 12, 14, 14),
                                       fixed=project is None)
        outer.addWidget(panel)

        title = self.name or "Unassigned"
        #: How this block's fold is remembered between sessions. By name, so a
        #: project keeps its own answer as others come and go.
        self.fold_key = ("project:%s" % self.name if project is not None
                         else "unassigned")
        self.disclosure = widgets.Disclosure(
            title, expanded=page.fold_state(
                self.fold_key,
                project.expanded if project is not None else True))
        self.disclosure.toggled.connect(self._remember_expanded)
        panel.layout().addWidget(self.disclosure)
        self._title = title

        if project is not None:
            self._add_project_buttons()
        body = self.disclosure.body()

        if project is not None:
            body.addWidget(widgets.row(widgets.kicker("Services (runners)"), None))
            self.runners = _table(self.RUNNER_HEADERS, self.R_ACTIONS,
                                  stretch=self.R_CONFIG)
            # Several at a time, and one click each way: the selection is what
            # narrows Start and Stop to part of a project, so making one asks for
            # no modifier key that nobody would think to hold.
            self.runners.setSelectionMode(QAbstractItemView.MultiSelection)
            self.runners.itemSelectionChanged.connect(self._selection_changed)
            self.runners.doubleClicked.connect(
                lambda index: self._console_at(index.row()))
            self._runners_note = widgets.empty_note(
                self.runners, "No services yet - Add Configuration builds one.")
            # No box over the criteria or the buttons: neither cell holds text,
            # so a search of them could only ever match nothing.
            self.runners_search = SearchRow(
                self.runners, skip=(self.R_CRITERIA, self.R_ACTIONS))
            self.runners_search.changed.connect(self._runners_filtered)
            body.addWidget(self._panel(self.runners))
        else:
            self.runners = None
            self.runners_search = None
            note = widgets.lede(
                "Logs that name no project, or one that is no longer configured. "
                "They stream exactly as they always have - give one a project in "
                "its Edit dialog to move it into a block above.")
            body.addWidget(note)

        self.open_full_button = _ghost(
            "Open Full", lambda: self.page.open_selected_log(self, True),
            "As much of the selected log as is sane to move and to render.")
        self.open_tail_button = _ghost(
            "Open Tail", lambda: self.page.open_selected_log(self, False),
            "The last few hundred lines of the selected log.")
        body.addWidget(widgets.row(
            widgets.kicker("Logs (observers)"), None, self.open_full_button,
            self.open_tail_button,
            _ghost("+ Add log", lambda: self.page.add_log(self.name))))
        self.logs = _table(self.LOG_HEADERS, self.L_ACTIONS, stretch=self.L_READS)
        self.logs.doubleClicked.connect(
            lambda index: self._edit_log_at(index.row()))
        # The unassigned block's empty state is a different claim from a
        # project's: there, nothing has been added yet; here, nothing is stray.
        self._logs_note = widgets.empty_note(self.logs,
                                             "No logs in this project yet."
                                             if project is not None else
                                             "No logs without a project.")
        self.logs_search = SearchRow(self.logs, skip=(self.L_ACTIONS,))
        self.logs_search.changed.connect(self._logs_filtered)
        body.addWidget(self._panel(self.logs))
        # Sets every button's label as well as whether it is enabled - the two
        # are the same question, asked of an empty selection.
        self._selection_changed()

    @staticmethod
    def _panel(table):
        panel = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        panel.layout().addWidget(table)
        return panel

    def _add_project_buttons(self):
        add = QPushButton("+ Add Configuration")
        add.setProperty("variant", "primary")
        add.setProperty("hasmenu", "true")
        menu = QMenu(add)
        for runner_type in runnertypes.TYPES:
            action = menu.addAction(icons.icon(runner_type.icon), runner_type.label)
            action.triggered.connect(
                lambda _checked=False, t=runner_type: self.page.add_runner(self.name, t))
        add.setMenu(menu)
        self._add_menu = menu           # kept alive with the button

        # These three act on the services, so an empty project has nothing for
        # them to do. Add Configuration is deliberately not among them: it is the
        # one thing an empty project is for.
        #
        # Each says "All" until rows are selected, and then says how many it will
        # act on instead. Selecting a row used to mean nothing here - the buttons
        # took the whole project either way - which made the selection look like
        # a thing that had stopped working.
        self._service_buttons = [
            (_ghost("Start All", lambda: self.page.start_project(
                self.name, self.selected_runners(), confirm=True), ""), "Start"),
            (_ghost("Stop All", lambda: self.page.stop_project(
                self.name, self.selected_runners(), confirm=True), ""), "Stop"),
            (_ghost("Restart All", lambda: self.page.restart_project(
                self.name, self.selected_runners(), confirm=True), ""), "Restart"),
        ]
        for widget in [button for button, _verb in self._service_buttons] + [
                _ghost("Edit", lambda: self.page.edit_project(self.name),
                       "Rename this project, or point it at another directory."),
                _ghost("Delete", lambda: self.page.delete_project(self.name)),
                add]:
            self.disclosure.add_to_header(widget)

    # -- selection ------------------------------------------------------------
    def selected_runners(self):
        """The services the block's buttons act on; empty means all of them.

        A row hidden by a search is not one of them even if it is still selected
        underneath: acting on something that cannot be seen is how a button ends
        up doing more than it said.
        """
        if self.runners is None:
            return []
        rows = sorted({index.row() for index in self.runners.selectedIndexes()})
        return [self._runner_rows[row].name for row in rows
                if 0 <= row < len(self._runner_rows)
                and not self.runners.isRowHidden(row)]

    def _selection_changed(self, *_args):
        chosen = len(self.selected_runners())
        for button, verb in self._service_buttons:
            button.setText("%s (%d)" % (verb, chosen) if chosen
                           else "%s All" % verb)
            button.setToolTip(
                "%s the %d selected service%s. Click a row again to drop it from "
                "the selection." % (verb, chosen, "" if chosen == 1 else "s")
                if chosen else
                "%s every service in this project - it asks first. Select rows "
                "to act on some of them instead." % verb)
        self.update_buttons()

    def _runners_filtered(self):
        self._after_filter(self.runners, self._runners_note, self.runners_search)
        # A search that hides a selected row changes what the buttons would act
        # on, and they have to say so.
        self._selection_changed()

    def _logs_filtered(self):
        self._after_filter(self.logs, self._logs_note, self.logs_search)

    @staticmethod
    def _after_filter(table, note, search):
        note.say_filtered("Nothing here matches the search."
                          if search.searching() else "")
        _fit_height(table)

    def search_state(self):
        """What is typed in this block's search boxes, to survive a rebuild.

        Editing a row rebuilds the page, and a search that cleared itself every
        time somebody acted on what they had found would be a search nobody could
        use twice.
        """
        return {"runners": (self.runners_search.state()
                            if self.runners_search is not None else {}),
                "logs": self.logs_search.state()}

    def restore_search(self, state):
        if self.runners_search is not None:
            self.runners_search.set_state((state or {}).get("runners"))
        self.logs_search.set_state((state or {}).get("logs"))

    def update_buttons(self):
        """Nothing offers to act on rows that are not there."""
        for button, _verb in self._service_buttons:
            button.setEnabled(bool(self._runner_rows))
        for button in (self.open_full_button, self.open_tail_button):
            button.setEnabled(bool(self._log_rows))

    def _remember_expanded(self, expanded):
        if self.project is not None:
            self.project.expanded = bool(expanded)
        self.page.remember_fold(self.fold_key, expanded)

    # -- runners --------------------------------------------------------------
    def _runner_at(self, index):
        return self._runner_rows[index] if 0 <= index < len(self._runner_rows) else None

    def _console_at(self, index):
        row = self._runner_at(index)
        if row is not None:
            self.page.open_console(self.name, row)

    def set_runners(self, rows):
        self._runner_rows = list(rows)
        table = self.runners
        if table is None:
            return
        table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            kind = row.runner_type
            label = runnertypes.label_of(row.type)
            if kind is not None and kind.detach_choice and row.detach:
                label += " · detached"
            if row.depends:
                # Two names and a count: a service waiting on five others would
                # otherwise push the column to its cap and take the width off
                # what the row actually runs. The full list is on the tooltip.
                shown = ", ".join(row.depends[:2])
                if len(row.depends) > 2:
                    shown += " +%d" % (len(row.depends) - 2)
                label += " · after %s" % shown
            _cell(table, index, self.R_NAME, row.name or "(unnamed)", mono=True)
            _cell(table, index, self.R_TYPE, label,
                  tip=("%s\nStarts after: %s" % (runnertypes.label_of(row.type),
                                                 ", ".join(row.depends))
                       if row.depends else label))
            _cell(table, index, self.R_CONFIG, row.summary() or "—", mono=True)
            _cell(table, index, self.R_STATUS, "")
            table.setCellWidget(index, self.R_CRITERIA, _criteria_strip(row.criteria))
            table.setCellWidget(index, self.R_ACTIONS, self._runner_actions(row))
        # A project that watches nothing looks exactly as it did before criteria
        # existed - an always-present empty column would be a standing question
        # about a feature most services never use.
        table.setColumnHidden(self.R_CRITERIA,
                              not any(row.criteria for row in rows))
        self._selection_changed()
        _fit_columns(table, self.R_ACTIONS)
        _fit_actions(table, self.R_CRITERIA)
        # New rows arrive shown; whatever is being searched for still holds.
        self.runners_search.apply()
        # After the content pass: the status cells are filled in later, by
        # set_status, so sizing this one to what is in it now would size it to
        # an empty string and clip every word it is about to hold.
        table.setColumnWidth(self.R_STATUS, _status_width(table))

    def _runner_actions(self, row):
        name = row.name
        toggle = _icon_button("run", lambda: self.page.toggle_runner(self.name, name),
                              "Start")
        restart = _icon_button("refresh",
                               lambda: self.page.restart_runner(self.name, name),
                               "Restart")
        strip = _actions(
            toggle, restart,
            _cell_button("Console", lambda: self.page.open_console(self.name, row),
                         "What this service is printing."),
            _cell_button("Edit", lambda: self.page.edit_runner(self.name, row)),
            _cell_button("Copy", lambda: self.page.copy_runner(self.name, row)),
            _cell_button("Delete", lambda: self.page.delete_runner(self.name, row)))
        strip.toggle = toggle
        return strip

    def set_status(self, runner_name, status, detail="", stale=False):
        """Repaint one row's status without rebuilding the table.

        Rebuilding on every status change would drop the selection and flicker
        the whole page every three seconds, which is how often the poll runs.
        """
        table = self.runners
        if table is None:
            return
        for index, row in enumerate(self._runner_rows):
            if row.name != runner_name:
                continue
            item = table.item(index, self.R_STATUS)
            if item is None:
                return
            text = STATUS_TEXT.get(status, status)
            if stale:
                # What is up is not what is written down: the settings changed
                # while it was running, and only a restart picks them up.
                text += " (edited)"
            item.setText(text)
            item.setIcon(icons.icon("dot", status_color(status)))
            item.setToolTip(detail or text)
            # Status is a column like any other, so a search on it has to follow
            # what the row now says - a service searched for as "failed" that has
            # since come up does not belong in the results.
            if self.runners_search.searching():
                self.runners_search.apply()
            strip = table.cellWidget(index, self.R_ACTIONS)
            toggle = getattr(strip, "toggle", None)
            if toggle is not None:
                running = status in (runnertypes.RUNNING, runnertypes.STARTING,
                                     runnertypes.STOPPING)
                icons.button(toggle, "stop" if running else "run", "")
                toggle.setToolTip("Stop" if running else "Start")
            return

    def set_criteria(self, runner_name, state):
        """Repaint one row's criteria. Never a rebuild - the poll is every 3 s."""
        table = self.runners
        if table is None:
            return
        for index, row in enumerate(self._runner_rows):
            if row.name != runner_name:
                continue
            strip = table.cellWidget(index, self.R_CRITERIA)
            tags = getattr(strip, "tags", None)
            if not tags or len(tags) != len(state):
                return          # the configuration moved under it; a rebuild follows
            for tag, (name, color, lit, outstanding) in zip(tags, state):
                tag.set_state(name, color, lit, outstanding)
                tag.setVisible(lit)
            strip.setToolTip(_watching_for(state))
            # The strip is only as wide as what is showing, so the column has to
            # be re-measured when a tag appears or the one that just lit is cut
            # off by a width decided when nothing had.
            _fit_actions(table, self.R_CRITERIA)
            return

    def set_summary(self, running, total, failed=0):
        """The count in the block's own title: `Storefront (3 of 5 running · 1 failed)`.

        The failures only when there are some. A block that says "0 failed"
        whenever all is well is a number to read on every glance to find out it
        is still nothing, which is the opposite of what a summary is for.
        """
        if self.project is None:
            return
        if not total:
            self.disclosure.set_title("%s  (nothing configured)" % self._title)
            return
        count = "%d of %d running" % (running, total)
        if failed:
            count += " · %d failed" % failed
        self.disclosure.set_title("%s  (%s)" % (self._title, count))

    # -- logs -----------------------------------------------------------------
    def selected_log(self):
        index = self.logs.currentRow()
        return self._log_rows[index] if 0 <= index < len(self._log_rows) else None

    def _edit_log_at(self, index):
        if 0 <= index < len(self._log_rows):
            self.page.edit_log(self._log_rows[index])

    def set_logs(self, rows):
        self._log_rows = list(rows)
        table = self.logs
        table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            _cell(table, index, self.L_NAME, row.name or "(unnamed)", mono=True)
            _cell(table, index, self.L_ENVS, row.envs_text() or "(none)", mono=True)
            _cell(table, index, self.L_READS,
                  "%s  %s" % (row.type, row.target or "—"), mono=True)
            _cell(table, index, self.L_CONN, row.connection or "(none)", mono=True)
            _cell(table, index, self.L_FORMAT, row.format_label())
            _cell(table, index, self.L_DEFAULT, "yes" if row.default else "")
            table.setCellWidget(index, self.L_ACTIONS, _actions(
                _cell_button("Edit", lambda r=row: self.page.edit_log(r)),
                _cell_button("Copy", lambda r=row: self.page.copy_log(r)),
                _cell_button("Delete", lambda r=row: self.page.delete_log(r))))
        self.update_buttons()
        _fit_columns(table, self.L_ACTIONS)
        self.logs_search.apply()


# ------------------------------------------------------------------------ page
class ServicesPage(QWidget):
    """The page: two files, one accordion, and a supervisor watching the lot."""

    saved = Signal()

    #: Where this page's fold states live in QSettings. Every section on the page
    #: is remembered - a project, the unassigned block, the connections - because
    #: a person who folds four projects away to work on the fifth has said
    #: something about how they want the page, and having to say it again every
    #: time the window opens is the same as not being listened to. It is kept out
    #: of the two documents on purpose: folding a block is not an edit to
    #: anybody's configuration, so it must neither light up Save nor need one.
    FOLDS = "services"

    def __init__(self, settings=None, parent=None):
        super().__init__(parent)
        # Its own when nobody hands one over, so the page works standalone (and
        # in a test) and still remembers what it folded.
        self._settings = settings if settings is not None else Settings()
        self._folds = self._settings.folds(self.FOLDS)
        self._log_path = ""
        self._services_path = ""
        self._connections = []
        self._logs = []
        self._projects = []
        self._log_fingerprint = None
        self._services_fingerprint = None
        # Set when a file on disk could not be read at all. Kept until a load
        # succeeds, because the alternative is an empty editor over a file full
        # of content: validation would call the empty document fine and Save
        # would quietly replace whatever could not be parsed.
        self._load_error = ""
        self._services_error = ""
        self._envs = []
        self._core = None
        self._viewers = []          # open log windows, kept from being collected
        self._consoles = []         # ditto, for service consoles
        self._developer = False
        self._migrating = False     # reading the old path, writing the new one
        self._configured_services = ""   # what Settings says, "" for the default
        self._blocks = {}           # project name ("" = unassigned) -> ProjectBlock
        self._connection_order = []  # drawn row -> its index in _connections
        # Save is for changes, not for re-writing the same file: it stays dark
        # until something on screen differs from what was loaded.
        self._baseline = None
        self._dirty = False
        self._valid = True

        self.supervisor = svc.ServiceSupervisor(self)
        self.supervisor.status_changed.connect(self._status_changed)
        self.supervisor.criteria_changed.connect(self._criteria_changed)

        column = QVBoxLayout(self)
        column.setContentsMargins(24, 20, 24, 20)
        column.setSpacing(6)

        self.start_all_button = _ghost(
            "Start All", lambda: self.start_project(None, confirm=True),
            "Start every service on this page that is not already up. It asks "
            "first - this one is every project.")
        self.stop_all_button = _ghost(
            "Stop All", lambda: self.stop_project(None, confirm=True),
            "Stop every service on this page. It asks first.")
        self.add_project_button = QPushButton("+ Add Project")
        self.add_project_button.clicked.connect(self.add_project)
        self.reload_button = QPushButton("Reload")
        self.reload_button.clicked.connect(
            lambda: self.load(self._log_path, self._configured_services))
        self.save_button = QPushButton("Save")
        self.save_button.setProperty("variant", "primary")
        self.save_button.clicked.connect(self.save)
        # The whole page in one line, beside its name. Every block carries its
        # own tally, but a page of folded blocks answers "is anything wrong"
        # only by being unfolded one at a time - which is the question this is.
        self.totals = QLabel("")
        self.totals.setTextFormat(Qt.RichText)
        column.addWidget(widgets.row(
            widgets.heading("Services & Logs"), self.totals, None,
            self.start_all_button, self.stop_all_button,
            self.add_project_button, self.reload_button, self.save_button))

        self.wording = widgets.Phrasing()
        _tail = ("project groups the services that make one thing work and the logs "
                 "they write. A service is started and stopped here; a log is read "
                 "wherever it lives, whether or not anything here started it.")
        column.addWidget(self.wording.text(
            widgets.lede(""),
            "Everything running on this machine, by project. A " + _tail,
            "Everything running on this machine, by project, plus the logs "
            "--server-log can stream. A " + _tail))
        column.addSpacing(10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        self._column = QVBoxLayout(inner)
        self._column.setContentsMargins(0, 0, 6, 0)
        self._column.setSpacing(12)
        scroll.setWidget(inner)
        column.addWidget(scroll, 1)

        # The projects go here, at the top - they are the page. Everything below
        # is added once and stays put, so the blocks are rebuilt by replacing the
        # run of widgets between the top and this marker.
        self._blocks_from = 0
        self._blocks_end = self._make_connections()

        self.status = QLabel("")
        self.status.setWordWrap(True)
        column.addWidget(self.status)
        # Built empty rather than bare: the inventory arrives from --describe on
        # a thread, so between the window opening and that answer there is a
        # stretch with no load() yet - and a page showing only a Connections
        # header reads as broken rather than as empty.
        self._rebuild()
        self._validate()

    def _make_connections(self):
        """The connections fold, under the projects rather than over them.

        A connection serves logs across every project, so it cannot live inside
        one. It is also the part nobody opens twice - and above the projects it
        pushed the subject of the page down the screen for no one's benefit.
        """
        self.connections_fold = widgets.Disclosure(
            "Connections", expanded=self.fold_state("connections", False))
        self.connections_fold.toggled.connect(
            lambda expanded: self.remember_fold("connections", expanded))
        self.connections_fold.add_to_header(
            _ghost("+ Add connection", self.add_connection))
        self.connections = _table(["Name", "Runs on", "Used by", ""], 3, stretch=1)
        self.connections.doubleClicked.connect(
            lambda index: self.edit_connection(self._at(index.row())))
        self._connections_note = widgets.empty_note(
            self.connections,
            "No connections yet - a log is read over one, local "
            "for this machine or ssh for anywhere else.")
        self.connections_search = SearchRow(self.connections, skip=(3,))
        self.connections_search.changed.connect(
            lambda: ProjectBlock._after_filter(self.connections,
                                               self._connections_note,
                                               self.connections_search))
        self.connections_fold.body().addWidget(ProjectBlock._panel(self.connections))
        holder = widgets.BlueprintPanel(padding=(14, 12, 14, 14), fixed=True)
        holder.layout().addWidget(self.connections_fold)
        self._column.addWidget(holder)
        self._column.addStretch(1)
        return holder

    # -- folds ----------------------------------------------------------------
    def fold_state(self, key, default):
        """Whether the section ``key`` opens up or shut.

        ``default`` is what to do when this window has never been told - for a
        project, what the file says; for the connections, shut. Once somebody
        folds it themselves, their answer is the one that is kept.
        """
        value = self._folds.get(key)
        return bool(default) if value is None else bool(value)

    def remember_fold(self, key, expanded):
        """Keep a fold across sessions. Written now, not at the next Save."""
        self._folds[key] = bool(expanded)
        self._settings.save_folds(self.FOLDS, self._folds)

    # -- wiring ---------------------------------------------------------------
    def set_core(self, core):
        """The core runner, for opening a log. Without one the button says so."""
        self._core = core

    def set_environments(self, envs):
        """The env values from --describe, so a log is bound by picking not typing."""
        self._envs = [e for e in envs if e]

    def set_developer_mode(self, enabled):
        self._developer = bool(enabled)
        self.wording.apply(enabled)

    def project_names(self):
        return [p.name for p in self._projects if p.name]

    def rows(self):
        return list(self._connections), list(self._logs), list(self._projects)

    def paths(self):
        """The two files this page edits. Shown in Settings, not on the page."""
        return self._log_path, self._services_path

    # -- loading / saving -----------------------------------------------------
    def load(self, path, services_path="", log_sources_path=""):
        """Read both files.

        ``path`` is what the core reported reading. Both of the others are
        settings and may be blank, in which case each falls back to its own
        default under the user's own directory - and each reads the older
        location once more, so an upgrade never looks like having lost the file.
        """
        read_logs, self._log_path = lsf.resolve_path(log_sources_path, path)
        self._migrating_logs = read_logs != self._log_path
        self._configured_services = services_path or ""
        read_from, self._services_path = sf.resolve_path(services_path,
                                                         self._log_path)
        self._migrating = read_from != self._services_path
        self._load_error = self._services_error = ""
        try:
            self._connections, self._logs = lsf.load(read_logs)
        except lsf.LogSourcesFileError as exc:
            self._connections, self._logs = [], []
            self._load_error = str(exc)
        try:
            self._projects = sf.load(read_from)
        except sf.ServicesFileError as exc:
            self._projects = []
            self._services_error = str(exc)
        self._log_fingerprint = lsf.fingerprint(self._log_path)
        self._services_fingerprint = sf.fingerprint(self._services_path)
        self._rebuild()
        self._mark_clean()
        # Only over a clean page. Where the file is going to be written is
        # housekeeping, and saying it on top of "this file is not valid JSON"
        # replaces the one message that has to be read with the one that does
        # not matter yet.
        if self._validate() and (self._migrating or self._migrating_logs):
            # Said rather than done behind their back: the old file is still
            # theirs, and Save is what decides anything. Both files can be in
            # this state at once, and naming only one of them would be worse
            # than naming neither.
            moving = []
            if self._migrating_logs:
                moving.append("logs read from %s" % read_logs)
            if self._migrating:
                moving.append("projects read from %s" % read_from)
            self._show_ok("%s - Save writes to %s."
                          % (" · ".join(moving).capitalize(),
                             " and ".join(sorted({self._log_path,
                                                  self._services_path}))))

    def save(self):
        if not self._log_path:
            self._show_problem("No log sources path configured.")
            return
        written = []
        if not self._load_error:
            if not self._confirm_overwrite(self._log_path, self._log_fingerprint,
                                           lsf.fingerprint):
                return
            try:
                lsf.save(self._log_path, self._connections, self._logs)
            except lsf.LogSourcesFileError as exc:
                self._show_problem(str(exc))
                return
            self._log_fingerprint = lsf.fingerprint(self._log_path)
            written.append("%d log(s)" % len(self._logs))
        if self._services_path and not self._services_error:
            if not self._confirm_overwrite(self._services_path,
                                           self._services_fingerprint, sf.fingerprint):
                return
            try:
                sf.save(self._services_path, self._projects)
                self._migrating = False
            except sf.ServicesFileError as exc:
                self._show_problem(str(exc))
                return
            self._services_fingerprint = sf.fingerprint(self._services_path)
            written.append("%d project(s)" % len(self._projects))
        self._mark_clean()
        self._show_ok("Saved %s (previous kept as .bak)." % " and ".join(written))
        self.saved.emit()

    def _confirm_overwrite(self, path, known, read):
        """Never write over a change somebody else made while this was open."""
        if known is None or read(path) == known:
            return True
        return QMessageBox.question(
            self, "File changed on disk",
            "%s changed since it was loaded here.\n\nOverwrite it with what is on "
            "screen?" % path,
            QMessageBox.Save | QMessageBox.Cancel,
            QMessageBox.Cancel) == QMessageBox.Save

    # -- rendering ------------------------------------------------------------
    def _rebuild(self):
        """Rebuild the blocks from the two documents.

        Only ever called when the *configuration* changes. A status change
        repaints one cell (see :meth:`_status_changed`) - rebuilding on every
        poll would drop the selection and flicker the page every three seconds.

        Newest first, everywhere: blocks, services and logs are shown in the
        order they were added, most recent at the top, which is where the thing
        somebody is working on is. The lists themselves are left in the file's
        order - see :func:`_newest_first` - so nothing about what starts before
        what changes with the view.
        """
        self._rebuild_connections()
        # What was being searched for, so acting on a row that was found does not
        # throw away the search that found it.
        searches = {name: block.search_state()
                    for name, block in self._blocks.items()}
        # Everything before the connections holder is a block from last time.
        while self._column.indexOf(self._blocks_end) > self._blocks_from:
            item = self._column.takeAt(self._blocks_from)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._blocks = {}

        at = self._blocks_from
        known = {project.name for project in self._projects}
        for project in _newest_first(self._projects):
            block = ProjectBlock(self, project)
            block.set_runners(_newest_first(project.runners))
            block.set_logs(_newest_first([row for row in self._logs
                                          if row.project == project.name]))
            block.restore_search(searches.get(project.name))
            self._blocks[project.name] = block
            self._column.insertWidget(at, block)
            at += 1

        # Nothing configured at all: say so on its own ground. Without this the
        # page shows only the parts that are always there - Connections, and the
        # block for logs belonging to no project - which reads as a page that
        # failed to draw rather than as one nobody has filled in yet. It stands
        # where the projects would be, so what is missing is missing in its place.
        if not self._projects:
            self._column.insertWidget(at, widgets.empty_zone(
                "No projects yet",
                "A project groups the services that make one thing work - a "
                "server, its database, the queue behind them - and the logs they "
                "write. Add Project starts one."))
            at += 1

        # A log naming a project that is not configured is still a log. It goes
        # in the trailing block rather than nowhere: silently hiding somebody's
        # row because a name no longer matches would look exactly like losing it.
        orphans = [row for row in self._logs if row.project not in known]
        if orphans:
            block = ProjectBlock(self, None)
            block.set_logs(_newest_first(orphans))
            block.restore_search(searches.get(""))
            self._blocks[""] = block
            self._column.insertWidget(at, block)

        self.supervisor.sync(self._projects)
        self._refresh_statuses()
        self._update_dirty()

    # -- unsaved changes ------------------------------------------------------
    def _comparable(self):
        """The two documents as one value, for telling edited from untouched.

        Exactly what would be written, minus each project's ``expanded``: folding
        a block is a view preference and is saved along with the next real edit,
        but it is not itself a reason to light up Save.
        """
        projects = []
        for project in self._projects:
            entry = project.to_entry()
            entry.pop("expanded", None)
            projects.append(entry)
        return json.dumps({"connections": [c.to_entry() for c in self._connections],
                           "logs": [row.to_entry() for row in self._logs],
                           "projects": projects},
                          sort_keys=True, default=str)

    def is_dirty(self):
        return self._dirty

    def _mark_clean(self):
        self._baseline = self._comparable()
        self._update_dirty()

    def _update_dirty(self):
        self._dirty = (self._baseline is not None
                       and self._comparable() != self._baseline)
        self.save_button.setProperty("dirty", "true" if self._dirty else "false")
        # A stylesheet rule keyed on a property is only re-read on re-polish.
        self.save_button.style().unpolish(self.save_button)
        self.save_button.style().polish(self.save_button)
        self._update_buttons()

    def _update_buttons(self):
        """Nothing offers to act on what is not there."""
        runners = sum(len(project.runners) for project in self._projects)
        self.start_all_button.setEnabled(bool(runners))
        self.stop_all_button.setEnabled(bool(runners))
        for block in self._blocks.values():
            block.update_buttons()
        savable = self._valid and self._dirty and not (self._load_error
                                                       or self._services_error)
        self.save_button.setEnabled(savable)
        self.save_button.setToolTip(
            "Unsaved changes" if self._dirty else "Nothing to save")

    def confirm_discard(self):
        """True when it is all right to walk away. Asks only if there is a reason."""
        if not self._dirty:
            return True
        return QMessageBox.question(
            self, "Unsaved changes",
            "Services & Logs has changes that are not saved.\n\nDiscard them?",
            QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Cancel) == QMessageBox.Discard

    def _rebuild_connections(self):
        table = self.connections
        table.setRowCount(len(self._connections))
        # Shown newest first like everything else, so the row a button acts on is
        # found by where the row *is* in the file, not by where it is drawn. The
        # order is kept because a double-click arrives as a drawn row and has to
        # be turned back into the other kind.
        self._connection_order = [self._connections.index(row)
                                  for row in _newest_first(self._connections)]
        for index, row in enumerate(_newest_first(self._connections)):
            at = self._connection_order[index]
            used = sum(1 for log in self._logs if log.connection == row.name)
            _cell(table, index, 0, row.name or "(unnamed)", mono=True)
            _cell(table, index, 1, row.describe(), mono=True)
            _cell(table, index, 2, "%d log%s" % (used, "" if used == 1 else "s"))
            table.setCellWidget(index, 3, _actions(
                _cell_button("Edit", lambda i=at: self.edit_connection(i)),
                _cell_button("Copy", lambda i=at: self.duplicate_connection(i)),
                _cell_button("Delete", lambda i=at: self.delete_connection(i))))
        _fit_columns(table, 3)
        self.connections_search.apply()
        self.connections_fold.set_title(
            "Connections  (%d)" % len(self._connections))

    def _at(self, drawn_row):
        """The connection a drawn row is: the page sorts, the file does not."""
        if 0 <= drawn_row < len(self._connection_order):
            return self._connection_order[drawn_row]
        return -1

    def _refresh_statuses(self):
        for project in self._projects:
            block = self._blocks.get(project.name)
            if block is None:
                continue
            for runner in project.runners:
                service = self.supervisor.service(project.name, runner.name)
                block.set_status(runner.name,
                                 service.status if service else runnertypes.STOPPED,
                                 service.detail if service else "",
                                 service.stale if service else False)
                if service is not None and runner.criteria:
                    block.set_criteria(runner.name, service.criteria_state())
            self._summarise(project.name)
        self._update_totals()

    def _summarise(self, project):
        """Put one block's tally in its title. The counts come off the supervisor."""
        block = self._blocks.get(project)
        if block is None:
            return
        running, total = self.supervisor.counts(project)
        block.set_summary(running, total, len(self.supervisor.failed(project)))

    def _update_totals(self):
        """The page's own line: everything configured here, however it is folded.

        Coloured the same as the Status column and the rail, and read from the
        theme every time rather than kept, because dark mode rewrites the
        palette in place under whatever is already on screen.
        """
        up, down = len(self.supervisor.running()), len(self.supervisor.failed())
        parts = []
        if up:
            parts.append("<span style='color: %s'>%d running</span>"
                         % (theme.OK, up))
        if down:
            parts.append("<span style='color: %s'>%d failed</span>"
                         % (theme.BAD, down))
        self.totals.setText(" · ".join(parts))

    def _criteria_changed(self, project, name):
        block = self._blocks.get(project)
        if block is not None:
            block.set_criteria(name, self.supervisor.criteria_state(project, name))

    def _status_changed(self, project, name, status):
        # The page's line first, and whatever else happens: it is about every
        # service configured here, including one whose block is not drawn yet.
        self._update_totals()
        block = self._blocks.get(project)
        if block is None:
            return
        service = self.supervisor.service(project, name)
        block.set_status(name, status, service.detail if service else "",
                         service.stale if service else False)
        self._summarise(project)
        if status == runnertypes.FAILED and service is not None and service.detail:
            self._show_problem("%s · %s: %s" % (project, name, service.detail))

    # -- stacks ---------------------------------------------------------------
    def _project(self, name):
        for project in self._projects:
            if project.name == name:
                return project
        return None

    def add_project(self):
        dialog = ProjectDialog(taken=self.project_names(), parent=self)
        if dialog.exec() == QDialog.Accepted:
            # Appended, and shown at the top: the file keeps the order things
            # were written in, the page shows the order they arrived in.
            self._projects.append(_stamped(dialog.value()))
            self._rebuild()
            self._validate()

    def edit_project(self, name):
        project = self._project(name)
        if project is None:
            return
        dialog = ProjectDialog(project, taken=self.project_names(), parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        updated = dialog.value()
        index = self._projects.index(project)
        self._projects[index] = updated
        # Logs name their stack by name; renaming one behind their back would
        # tip every log that used it into the Unassigned block.
        if name and updated.name != name:
            for row in self._logs:
                if row.project == name:
                    row.project = updated.name
        self._rebuild()
        self._validate()

    def delete_project(self, name):
        project = self._project(name)
        if project is None:
            return
        running = [s for s in self.supervisor.detained() if s.project == name]
        logs = [row for row in self._logs if row.project == name]
        extra = ""
        if logs:
            extra += ("\n\n%d log(s) belong to it. They are kept and move to "
                      "Unassigned." % len(logs))
        if running:
            extra += ("\n\n%d service(s) are running and will be stopped."
                      % len(running))
        if QMessageBox.question(
                self, "Delete project",
                "Remove the project %r and its %d service(s)?%s"
                % (name or "(unnamed)", len(project.runners), extra),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        for row in logs:
            row.project = ""
        self._projects.remove(project)
        self._rebuild()
        self._validate()

    # ``names`` is what the block's selection says, and empty means the whole
    # project - which is also what the page's own Start All / Stop All pass.
    #
    # ``confirm`` is what a *button* passes. Only the All form asks: a button
    # that names a count was aimed at those rows by hand, while one that says
    # All can take down every backend on the machine from a click meant for the
    # row underneath it. A call made in code is not a click and asks nothing.
    def start_project(self, name=None, names=(), confirm=False):
        if not names and confirm and not self._confirm_all("start", name):
            return
        self.supervisor.start_all(name, names or None)

    def stop_project(self, name=None, names=(), confirm=False):
        if not names and confirm and not self._confirm_all("stop", name):
            return
        self.supervisor.stop_all(name, names or None)

    def restart_project(self, name=None, names=(), confirm=False):
        if not names and confirm and not self._confirm_all("restart", name):
            return
        self.supervisor.restart_all(name, names or None)

    #: For each verb: how many services it would actually touch, what to ask, and
    #: what to say instead when the answer is none of them. Named fields rather
    #: than positional, so each sentence names only what it has anything to say
    #: about - Start is the only one with a word for the services it leaves alone.
    _ALL_ACTIONS = {
        "start": (lambda running, total: total - running,
                  "Start every service {where}?\n\n{affected} will be started; "
                  "{running} already running.",
                  "Every service {where} is already running."),
        "stop": (lambda running, _total: running,
                 "Stop every service {where}?\n\n{affected} running service(s) "
                 "will be stopped.",
                 "No service {where} is running."),
        "restart": (lambda _running, total: total,
                    "Restart every service {where}?\n\n{affected} service(s) will "
                    "be stopped and started again.",
                    "There is no service {where} to restart."),
    }

    def _confirm_all(self, verb, project):
        """Ask before acting on a whole project, or on the whole page.

        Answers False when there is nothing for the verb to do, having said so:
        an action that would touch nothing is not worth a question.
        """
        counted, question, nothing = self._ALL_ACTIONS[verb]
        running, total = self.supervisor.counts(project)
        where = "in %r" % project if project else "on this page"
        affected = counted(running, total)
        if not affected:
            self._show_ok(nothing.format(where=where))
            return False
        return QMessageBox.question(
            self, "%s all services" % verb.capitalize(),
            question.format(where=where, affected=affected, running=running,
                            total=total),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No) == QMessageBox.Yes

    # -- runners --------------------------------------------------------------
    def add_runner(self, project_name, runner_type):
        project = self._project(project_name)
        if project is None:
            return
        dialog = RunnerDialog(runner_type,
                              taken=[r.name for r in project.runners],
                              siblings=[r.name for r in project.runners],
                              project_dir=project.dir, parent=self)
        if dialog.exec() == QDialog.Accepted:
            project.runners.append(_stamped(dialog.value()))
            self._rebuild()
            self._validate()

    def edit_runner(self, project_name, row):
        project = self._project(project_name)
        if project is None or row not in project.runners:
            return
        kind = row.runner_type
        if kind is None:
            self._show_problem("This build has no %r runner, so there is no form "
                               "for it. The row is kept as it is." % row.type)
            return
        dialog = RunnerDialog(kind, row, taken=[r.name for r in project.runners],
                              siblings=[r.name for r in project.runners],
                              project_dir=project.dir, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        updated = dialog.value()
        project.runners[project.runners.index(row)] = updated
        # Services wait for each other by name; renaming one behind their back
        # would leave them waiting for something that no longer exists.
        if row.name and updated.name != row.name:
            for other in project.runners:
                other.depends = [updated.name if d == row.name else d
                                 for d in other.depends]
        self._rebuild()
        self._validate()

    def copy_runner(self, project_name, row):
        project = self._project(project_name)
        if project is None or row not in project.runners:
            return
        clone = row.copy()
        clone.name = _unique(clone.name or "service",
                             [r.name for r in project.runners])
        # A copy is a row that did not exist a moment ago, so it is the newest
        # one whatever the row it was taken from says.
        clone.added = lsf.stamp()
        project.runners.insert(project.runners.index(row) + 1, clone)
        self._rebuild()
        self._validate()

    def delete_runner(self, project_name, row):
        project = self._project(project_name)
        if project is None or row not in project.runners:
            return
        service = self.supervisor.service(project_name, row.name)
        note = ""
        if service is not None and service.is_running():
            note = ("\n\nIt is running. It will be stopped." if service.detained()
                    else "\n\nIt is running, and was set to keep running on its "
                         "own - deleting the row does not stop it.")
        if QMessageBox.question(
                self, "Delete service", "Remove %r from %r?%s"
                % (row.name or "(unnamed)", project_name, note),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        project.runners.remove(row)
        for other in project.runners:
            other.depends = [d for d in other.depends if d != row.name]
        self._rebuild()
        self._validate()

    def toggle_runner(self, project_name, runner_name):
        service = self.supervisor.service(project_name, runner_name)
        if service is None:
            return
        if service.is_running():
            self.supervisor.stop(project_name, runner_name)
        else:
            self._show_ok("Starting %s…" % runner_name)
            self.supervisor.start(project_name, runner_name)

    def restart_runner(self, project_name, runner_name):
        self.supervisor.restart(project_name, runner_name)

    def open_console(self, project_name, row):
        service = self.supervisor.service(project_name, row.name)
        if service is None:
            self._show_problem("No service for %r yet - give it a name first."
                               % (row.name or "(unnamed)"))
            return
        window = ConsoleWindow(service, parent=self)
        self._consoles.append(window)
        window.finished.connect(
            lambda _code, w=window: self._consoles.remove(w)
            if w in self._consoles else None)
        window.show()
        window.raise_()
        return window

    # -- connections ----------------------------------------------------------
    def add_connection(self):
        dialog = ConnectionDialog(taken=[c.name for c in self._connections],
                                  parent=self)
        if dialog.exec() == QDialog.Accepted:
            self._connections.append(_stamped(dialog.value()))
            self.connections_fold.set_expanded(True)
            self._rebuild()
            self._validate()

    def edit_connection(self, index):
        if not (0 <= index < len(self._connections)):
            return
        previous = self._connections[index].name
        dialog = ConnectionDialog(self._connections[index],
                                  taken=[c.name for c in self._connections],
                                  parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        self._connections[index] = dialog.value()
        # Logs refer to a connection by name; renaming one behind their back
        # would orphan every log that used it.
        renamed = self._connections[index].name
        if previous and renamed != previous:
            for row in self._logs:
                if row.connection == previous:
                    row.connection = renamed
        self._rebuild()
        self._validate()

    def duplicate_connection(self, index):
        if not (0 <= index < len(self._connections)):
            return
        clone = self._connections[index].copy()
        clone.name = _unique(clone.name or "connection",
                             [c.name for c in self._connections])
        clone.added = lsf.stamp()
        self._connections.insert(index + 1, clone)
        self._rebuild()
        self._validate()

    def delete_connection(self, index):
        if not (0 <= index < len(self._connections)):
            return
        row = self._connections[index]
        used = [log.name for log in self._logs if log.connection == row.name]
        extra = ("\n\n%d log(s) use it: %s. They will have no connection until you "
                 "point them somewhere else." % (len(used), ", ".join(used))
                 if used else "")
        if QMessageBox.question(
                self, "Delete connection",
                "Remove connection %r?%s" % (row.name or "(unnamed)", extra),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        del self._connections[index]
        self._rebuild()
        self._validate()

    # -- logs -----------------------------------------------------------------
    def add_log(self, project_name=""):
        if not self._connections:
            self.connections_fold.set_expanded(True)
            self._show_problem("Add a connection first - a log has to be read over "
                               "one.")
            return
        seed = lsf.LogRow(connection=self._connections[0].name,
                          envs=self._envs[:1], type="file", project=project_name)
        dialog = LogDialog(seed, connections=self._connections, envs=self._envs,
                           siblings=self._logs, developer=self._developer,
                           projects=self.project_names(), parent=self)
        if dialog.exec() == QDialog.Accepted:
            self._logs.append(_stamped(dialog.value()))
            self._rebuild()
            self._validate()

    def edit_log(self, row):
        if row not in self._logs:
            return
        others = [other for other in self._logs if other is not row]
        dialog = LogDialog(row, connections=self._connections, envs=self._envs,
                           siblings=others, developer=self._developer,
                           projects=self.project_names(), parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        self._logs[self._logs.index(row)] = dialog.value()
        self._rebuild()
        self._validate()

    def copy_log(self, row):
        if row not in self._logs:
            return
        clone = row.copy()
        clone.name = _unique(clone.name or "log", [r.name for r in self._logs])
        clone.added = lsf.stamp()
        self._logs.insert(self._logs.index(row) + 1, clone)
        self._rebuild()
        self._validate()

    def delete_log(self, row):
        if row not in self._logs:
            return
        if QMessageBox.question(
                self, "Delete log", "Remove log %r?" % (row.name or "(unnamed)"),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self._logs.remove(row)
        self._rebuild()
        self._validate()

    def open_selected_log(self, block, whole):
        """Show what the selected log holds - the whole of it, or its tail."""
        row = block.selected_log()
        if row is None:
            self._show_problem("Select a log to open.")
            return
        if not row.name:
            self._show_problem("Give the log a name first.")
            return
        if self._core is None:
            self._show_problem("No core configured (Settings).")
            return
        if lsf.validate(self._connections, self._logs):
            self._show_problem("Fix the problems below and save first - the "
                               "launcher reads the file, not the screen.")
            return
        if self._log_fingerprint != lsf.fingerprint(self._log_path):
            self._show_problem("Save first: the launcher reads what is in the file.")
            return
        self._show_ok("Opening %s…" % row.name)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            result = self._core.server_log_show(row.name,
                                                lines=None if whole else 500)
        finally:
            QApplication.restoreOverrideCursor()
        if not result.get("ok"):
            self._show_problem("%s: %s" % (row.name, result.get("error")
                                           or "could not be read"))
            return
        self._show_ok("%s: %d line(s) from %s"
                      % (row.name, len(result.get("lines") or []),
                         result.get("target", "")))
        viewer = LogViewerDialog(row.name, result, whole, parent=self)
        self._viewers.append(viewer)
        viewer.finished.connect(
            lambda _code, v=viewer: self._viewers.remove(v)
            if v in self._viewers else None)
        viewer.show()
        viewer.raise_()
        return viewer

    # -- validation -----------------------------------------------------------
    def _validate(self):
        if self._load_error or self._services_error:
            # Never offer to save over a file that could not be read: an empty
            # editor validates perfectly, and saving it would throw the content
            # away. Reload once the file is fixed.
            self._show_problem("%s\n\nFix the file and press Reload - saving now "
                               "would replace it with an empty one."
                               % (self._load_error or self._services_error))
            self._valid = False
            self._update_buttons()
            return False
        problems = (lsf.validate(self._connections, self._logs)
                    + sf.validate(self._projects))
        if problems:
            self._show_problem("\n".join(problems[:6]))
        elif not self._projects and not self._logs:
            self._show_ok("Nothing configured yet. Add a project, then a service in "
                          "it - or a connection and a log, to read a backend "
                          "nothing here starts.")
        else:
            runners = sum(len(p.runners) for p in self._projects)
            self._show_ok("%d project(s) · %d service(s) · %d connection(s) · %d log(s)"
                          % (len(self._projects), runners, len(self._connections),
                             len(self._logs)))
        self._valid = not problems
        self._update_buttons()
        return not problems

    def _show_problem(self, message):
        self.status.setText(message)
        self.status.setStyleSheet("color: %s; font-size: 12px;" % theme.BAD)

    def _show_ok(self, message):
        self.status.setText(message)
        self.status.setStyleSheet("color: %s; font-size: 12px;" % theme.NEUTRAL[700])

    # -- closing --------------------------------------------------------------
    def detained(self):
        """Running services that would be lost if the window closed now."""
        return self.supervisor.detained()

    def shutdown(self, timeout_ms=svc.SHUTDOWN_WAIT_MS):
        return self.supervisor.shutdown(timeout_ms)

    def closeEvent(self, event):
        # Closing the page stops watching, not the services: whether they end is
        # decided in the main window's closeEvent, by whether each may detach.
        self.supervisor.dispose()
        super().closeEvent(event)


def _unique(name, taken):
    """A copy's name, so a duplicate is valid the moment it is made."""
    candidate, index = "%s-copy" % name, 2
    while candidate in taken:
        candidate, index = "%s-copy-%d" % (name, index), index + 1
    return candidate
