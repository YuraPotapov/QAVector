"""The accounts table on Launch Sessions: its geometry, its two modes, its gaps.

Tables are where columns, titles and the boxes over them drifted apart before,
so the edges that are meant to line up are measured; and every state the table
can be left in with nothing to show says so.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QPoint, Qt

from cms_gui import theme
from cms_gui.choicetable import CHECK_WIDTH, NO_MATCH, indicator_rect
from cms_gui.pages.launchaccounts import (NO_ACCOUNTS, NOT_HERE, NOTHING_SELECTED,
                                          AccountsTable)

ROWS = [{"login": "admin", "role": "Admin", "scenarios": ["shop_order"]},
        {"login": "operator@example.invalid", "role": "Operator", "scenarios": []},
        {"login": "agent@example.invalid", "role": "", "scenarios": ["tag:smoke"]}]


@pytest.fixture
def table(qapp):
    widget = AccountsTable()
    widget.resize(520, 320)
    widget.show()
    qapp.processEvents()
    widget.set_rows(ROWS, ["admin"])
    qapp.processEvents()
    yield widget
    widget.close()


# ------------------------------------------------------------ the geometry

def test_the_header_box_sits_exactly_over_the_row_boxes(table):
    view, header = table.table, table.header
    section = header.rect()
    section.setLeft(header.sectionViewportPosition(0))
    section.setWidth(header.sectionSize(0))
    over = indicator_rect(section)
    over_x = header.viewport().mapTo(view, QPoint(over.x(), 0)).x()
    for row in range(view.rowCount()):
        under = indicator_rect(view.visualRect(view.model().index(row, 0)))
        assert view.viewport().mapTo(view, QPoint(under.x(), 0)).x() == over_x
        assert under.width() == over.width() == theme.INDICATOR


def test_the_box_has_the_same_room_either_side(table):
    cell = table.table.visualRect(table.table.model().index(0, 0))
    box = indicator_rect(cell)
    assert box.left() - cell.left() == (cell.left() + CHECK_WIDTH) - (box.left() + box.width())


def test_search_boxes_sit_on_their_columns_edges_after_the_rows_change(table, qapp):
    """The rows arrive after the table is on screen; the boxes must follow."""
    table.set_rows(ROWS + [{"login": "a-much-longer-login@example.invalid",
                            "role": "Supervisor", "scenarios": []}])
    qapp.processEvents()
    header = table.header
    assert table.search.box(0) is None
    for column in range(1, table.table.columnCount()):
        box = table.search.box(column)
        assert box.geometry().x() == header.sectionViewportPosition(column), column
        assert box.geometry().width() == header.sectionSize(column) + 1, column


def test_every_row_is_as_tall_as_the_next(table):
    heights = {table.table.rowHeight(row) for row in range(table.table.rowCount())}
    assert len(heights) == 1


def test_the_layout_is_the_same_whichever_mode(table, qapp):
    before = [table.header.sectionSize(c) for c in range(table.table.columnCount())]
    table.set_locked(True)
    qapp.processEvents()
    assert [table.header.sectionSize(c) for c in range(table.table.columnCount())] == before


# ------------------------------------------------------------ the two modes

def test_all_accounts_ticks_every_box_and_keeps_the_pick(table):
    table.set_locked(True)
    assert all(table.is_checked(row) for row in range(3))
    table.toggle(1)                         # deaf while every account goes
    assert table.checked() == ["admin"]
    assert table.count.text() == "All 3 go"
    table.set_locked(False)
    assert [table.is_checked(row) for row in range(3)] == [True, False, False]


def test_a_tick_is_one_account_even_when_the_login_shows_twice(table):
    table.set_rows(ROWS + [{"login": "admin", "role": "Admin (other)", "scenarios": []}])
    table.toggle(0)
    assert table.is_checked(0) and table.is_checked(3)
    assert table.checked() == ["admin"]


def test_the_header_box_ticks_what_is_shown(table):
    table.toggle_all()
    assert table.checked() == [row["login"] for row in ROWS]
    assert table.all_state() == Qt.Checked
    table.toggle_all()
    assert table.checked() == []


def test_the_count_says_what_is_chosen_out_of_what_there_is(table):
    assert table.count.text() == "Selected 1 / Available 3"


# ------------------------------------------------------------ the gaps

def test_an_environment_with_no_accounts_says_how_to_add_one(table):
    table.set_rows([])
    assert NO_ACCOUNTS in _said(table)


def test_a_search_that_hides_every_row_says_so_and_counts_hidden_ticks(table, qapp):
    table.search.box(2).setText("nobody")
    qapp.processEvents()
    assert NO_MATCH in _said(table)
    assert "1 ticked out of sight" in table.count.text()


def test_selected_with_nothing_ticked_says_where_to_tick(table):
    table.set_checked([])
    table.view.set_current("Selected")
    assert NOTHING_SELECTED in _said(table)
    table.view.set_current("All")
    assert _said(table) == ""


def test_selected_shows_only_the_ticked_rows(table):
    table.view.set_current("Selected")
    shown = [row for row in range(3) if not table.table.isRowHidden(row)]
    assert shown == [0]


def test_an_account_the_environment_does_not_have_is_shown_not_dropped(table):
    table.set_rows(ROWS + [{"login": "ghost", "role": "", "scenarios": [],
                            "missing": True}], ["ghost"])
    assert table.table.item(3, 3).text() == NOT_HERE
    assert table.checked() == ["ghost"]
    assert table.count.text().endswith("Available 3")


def test_an_empty_cell_is_a_dash(table):
    assert table.table.item(2, 1).text() == "—"
    assert table.table.item(1, 3).text() == "—"


def _said(table):
    """What the table's own note says - "" while rows are shown and it is not."""
    note = table._note
    return note.text() if not note.isHidden() else ""
