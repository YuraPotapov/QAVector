"""The table a ``user.choice`` step is answered in.

Two kinds of thing are worth defending here. The first is the geometry: tables
are where columns, their titles and the boxes over them drifted apart before,
so every edge that is meant to line up is measured - the tick boxes under the
header's box, each title over its values, each search box over its column -
rather than taken on trust from a screenshot. The second is the gaps: a cell
with nothing in it, a row with no detail, nothing selected, a search that
hides every row. Each is a state the window shows on purpose.
"""

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QHeaderView

from cms_gui import theme
from cms_gui.askbridge import CHOICE_DETAIL_HEIGHT, AskDialog
from cms_gui.choicetable import (CHECK_WIDTH, EMPTY, NO_MATCH, ChoiceTable,
                                 indicator_rect, text_inset)

TASKS = [("QA-1", "Fix the login form", "To Do", "Ann"),
         ("QA-2", "Add a filter", "REOPEN", ""),
         ("QA-3", "Export to CSV", "To Do", "Bob"),
         ("QA-4", "A long title that has a great deal more to say than the rest", "To Do", "Ann"),
         ("QA-5", "", "To Do", "Bob")]


def _choice(count=5, mode="multiple", checked=(), details=True, columns=None):
    rows = []
    for index in range(count):
        key, title, status, who = TASKS[index % len(TASKS)]
        key = key if index < len(TASKS) else "QA-%d" % (100 + index)
        rows.append({"cells": [key, title, status, who],
                     "detail": ("%s\n\nAbout %s." % (status, key)) if details and index != 1 else "",
                     "checked": index in checked})
    return {"mode": mode, "columns": columns or ["key", "summary", "status", "assignee"],
            "value_column": 0, "rows": rows}


def _shown(qapp, widget, width=900, height=520):
    widget.resize(width, height)
    widget.show()
    qapp.processEvents()
    return widget


def _first_ink(image, left, right, top, bottom):
    """The first column of pixels, left to right, that differs from the ground."""
    ground = QColor(image.pixel(left, (top + bottom) // 2))
    for x in range(left, right):
        for y in range(top, bottom):
            one = QColor(image.pixel(x, y))
            if (abs(one.red() - ground.red()) + abs(one.green() - ground.green())
                    + abs(one.blue() - ground.blue())) > 150:
                return x
    return None


# ------------------------------------------------------------ the geometry
def test_the_header_box_sits_exactly_over_the_row_boxes(qapp):
    table = _shown(qapp, ChoiceTable(_choice(checked=(0,))))
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


def test_the_box_is_centred_with_the_same_room_either_side(qapp):
    table = _shown(qapp, ChoiceTable(_choice()))
    cell = table.table.visualRect(table.table.model().index(0, 0))
    box = indicator_rect(cell)
    assert box.left() - cell.left() == (cell.left() + CHECK_WIDTH) - (box.left() + box.width())


def test_each_title_starts_where_its_values_do(qapp):
    """Centred titles over left-aligned values were the asymmetry: each title
    is now given the cell's own inset, the variables table's rule."""
    table = _shown(qapp, ChoiceTable(_choice(columns=["h", "hat"])))
    table.rows[0]["cells"] = ["Hill", "Home"]
    table.table.item(0, 1).setText("Hill")
    table.table.item(0, 2).setText("Home")
    qapp.processEvents()
    view, header = table.table, table.header
    assert header.defaultAlignment() & Qt.AlignLeft
    image = view.grab().toImage()
    rect = view.visualRect(view.model().index(0, 1))
    top = view.viewport().mapTo(view, rect.topLeft()).y()
    for column in (1, 2):
        left = view.viewport().mapTo(view, QPoint(view.columnViewportPosition(column), 0)).x()
        value = _first_ink(image, left + 2, left + 40, top + 4, top + rect.height() - 4)
        title = _first_ink(image, left + 2, left + 40, header.y() + 5,
                           header.y() + header.height() - 5)
        assert value is not None and title is not None
        assert abs(value - title) <= 1, (column, value - left, title - left)


def test_the_header_is_told_the_cells_inset(qapp):
    table = _shown(qapp, ChoiceTable(_choice()))
    assert "padding-left: %dpx" % text_inset(table.table) in table.header.styleSheet()


def test_search_boxes_share_their_columns_edges_and_none_is_over_the_ticks(qapp):
    table = _shown(qapp, ChoiceTable(_choice(count=20)))
    search, header = table.search, table.header
    assert search is not None
    assert search.box(0) is None
    for column in range(1, table.table.columnCount()):
        box = search.box(column)
        assert box.geometry().x() == header.sectionViewportPosition(column)
        assert box.geometry().width() == header.sectionSize(column) + 1


def test_typed_text_starts_where_the_column_s_values_do(qapp):
    table = _shown(qapp, ChoiceTable(_choice(count=20, columns=["h", "hat"])))
    view = table.table
    for row in range(view.rowCount()):
        view.item(row, 2).setText("Home")
    box = table.search.box(2)
    box.setText("Ho")
    qapp.processEvents()
    image = view.grab().toImage()
    left = view.viewport().mapTo(view, QPoint(view.columnViewportPosition(2), 0)).x()
    rect = view.visualRect(view.model().index(0, 2))
    top = view.viewport().mapTo(view, rect.topLeft()).y()
    value = _first_ink(image, left + 2, left + 40, top + 4, top + rect.height() - 4)
    origin = box.mapTo(view, QPoint(0, 0))
    typed = _first_ink(image, origin.x() + 2, origin.x() + 40,
                       origin.y() + 3, origin.y() + box.height() - 3)
    assert value is not None and typed is not None
    assert abs(value - typed) <= 1, (value - left, typed - left)


def test_the_tick_column_is_fixed_and_the_widest_column_stretches(qapp):
    table = _shown(qapp, ChoiceTable(_choice()))
    header = table.header
    assert header.sectionResizeMode(0) == QHeaderView.Fixed
    assert header.sectionSize(0) == CHECK_WIDTH
    assert header.sectionResizeMode(2) == QHeaderView.Stretch      # summary
    for column in (1, 3, 4):
        assert header.sectionResizeMode(column) == QHeaderView.Interactive


def test_every_row_is_the_same_height(qapp):
    table = _shown(qapp, ChoiceTable(_choice(count=12)))
    heights = {table.table.rowHeight(row) for row in range(table.table.rowCount())}
    assert len(heights) == 1


def test_single_and_multiple_share_one_layout(qapp):
    many = _shown(qapp, ChoiceTable(_choice()))
    one = _shown(qapp, ChoiceTable(_choice(mode="single")))
    widths = [[table.header.sectionSize(column) for column in range(table.table.columnCount())]
              for table in (many, one)]
    assert widths[0] == widths[1]
    assert many.header.height() == one.header.height()
    assert many.table.rowHeight(0) == one.table.rowHeight(0)


def test_four_columns_need_no_sideways_scroll_in_the_window(qapp):
    dialog = AskDialog("Which?", options=["Choose", "Cancel"], choice=_choice(count=30))
    dialog.show()
    qapp.processEvents()
    try:
        assert dialog.choice_table.table.horizontalScrollBar().maximum() == 0
    finally:
        dialog.close()


# ------------------------------------------------------ a long list scrolls
def test_a_long_list_scrolls_inside_the_table_and_the_window_fits_the_screen(qapp):
    dialog = AskDialog("Which?", detail="A note.", options=["Choose", "Cancel"],
                       choice=_choice(count=1000))
    dialog.show()
    qapp.processEvents()
    try:
        available = dialog.screen().availableGeometry()
        assert dialog.height() <= available.height() - 48
        assert dialog.choice_table.table.verticalScrollBar().maximum() > 0
    finally:
        dialog.close()


def test_the_window_needs_no_more_than_a_small_screen_gives_it(qapp):
    """A 600px screen, less the 48 the window keeps clear: everything past the
    least the table and the detail need scrolls, so the window never runs off
    the bottom of the screen."""
    for detail in ("", "A note above the table."):
        dialog = AskDialog("Which tasks go into this release?", detail=detail,
                           options=["Choose", "Cancel"], choice=_choice(count=1000))
        dialog.show()
        qapp.processEvents()
        try:
            assert dialog.minimumSizeHint().height() <= 600 - 48
        finally:
            dialog.close()


def test_a_short_note_is_one_line_and_a_long_one_scrolls_in_its_box(qapp):
    short = AskDialog("Which?", detail="One line.", options=["Choose", "Cancel"],
                      choice=_choice())
    long = AskDialog("Which?", detail="\n".join("line %d" % one for one in range(60)),
                     options=["Choose", "Cancel"], choice=_choice())
    for dialog in (short, long):
        dialog.show()
    qapp.processEvents()
    try:
        assert short.choice_note.height() < CHOICE_DETAIL_HEIGHT / 2
        assert long.choice_note.height() == CHOICE_DETAIL_HEIGHT
        assert long.choice_note.verticalScrollBar().maximum() > 0
    finally:
        short.close()
        long.close()


# ------------------------------------------------------------ the gaps
def test_an_empty_cell_is_a_muted_dash_with_nothing_on_hover(qapp):
    table = _shown(qapp, ChoiceTable(_choice()))
    item = table.table.item(1, 4)                   # QA-2 has no assignee
    assert item.text() == EMPTY
    assert item.toolTip() == ""
    assert item.foreground().color() == theme.color(theme.NEUTRAL[500])
    assert table.table.item(0, 4).toolTip() == "Ann"


def test_the_value_column_is_machine_text(qapp):
    table = _shown(qapp, ChoiceTable(_choice()))
    assert table.table.item(0, 1).font().family() == theme.mono_font().family()
    assert table.table.item(0, 2).font().family() != theme.mono_font().family()


def test_nothing_selected_is_a_state_of_its_own(qapp):
    table = _shown(qapp, ChoiceTable(_choice()))
    assert table.detail_stack.currentIndex() == 0


def test_the_first_ticked_row_is_shown_when_it_opens(qapp):
    table = _shown(qapp, ChoiceTable(_choice(checked=(2,))))
    assert table.detail_stack.currentIndex() == 1
    assert table.detail_title.text() == "QA-3"
    assert table.detail_text.toPlainText() == "To Do\n\nAbout QA-3."


def test_a_row_with_no_detail_says_so_in_the_same_box(qapp):
    table = _shown(qapp, ChoiceTable(_choice()))
    table.table.selectRow(1)
    qapp.processEvents()
    assert table.detail_stack.currentIndex() == 1
    assert table.detail_text.toPlainText() == ""
    assert table.detail_text.placeholderText() == "No details for this item."


def test_a_search_that_hides_every_row_says_so_and_counts_what_is_shown(qapp):
    table = _shown(qapp, ChoiceTable(_choice(count=20, checked=(0, 1))))
    table.search.box(2).setText("nothing like this")
    qapp.processEvents()
    assert table._note.isVisible()
    assert table._note.text() == NO_MATCH
    assert table.count.text() == "2 of 20 chosen, 0 shown"
    assert table.detail_stack.currentIndex() == 0


# ------------------------------------------------------------ ticking
def test_the_header_box_ticks_every_shown_row_then_clears_them(qapp):
    table = _shown(qapp, ChoiceTable(_choice(count=20)))
    assert table.all_state() == Qt.Unchecked
    table.set_checked(3)
    assert table.all_state() == Qt.PartiallyChecked
    table.toggle_all()
    assert table.chosen() == list(range(20))
    assert table.all_state() == Qt.Checked
    table.toggle_all()
    assert table.chosen() == []


def test_the_header_box_leaves_rows_a_search_hides_alone(qapp):
    table = _shown(qapp, ChoiceTable(_choice(count=20, checked=(1,))))
    table.search.box(1).setText("QA-1")              # QA-1, and QA-1xx
    qapp.processEvents()
    shown = table._shown()
    table.toggle_all()
    assert set(table.chosen()) == set(shown) | {1}


def test_clicking_the_header_box_ticks_all(qapp):
    table = _shown(qapp, ChoiceTable(_choice()))
    header = table.header
    centre = QPoint(header.sectionViewportPosition(0) + header.sectionSize(0) // 2,
                    header.height() // 2)
    QTest.mouseClick(header.viewport(), Qt.LeftButton, Qt.NoModifier, centre)
    qapp.processEvents()
    assert table.chosen() == list(range(5))


def test_in_multiple_mode_reading_a_row_does_not_tick_it(qapp):
    table = _shown(qapp, ChoiceTable(_choice()))
    view = table.table
    QTest.mouseClick(view.viewport(), Qt.LeftButton, Qt.NoModifier,
                     view.visualRect(view.model().index(2, 2)).center())
    qapp.processEvents()
    assert table.chosen() == []
    assert table.detail_title.text() == "QA-3"
    QTest.mouseClick(view.viewport(), Qt.LeftButton, Qt.NoModifier,
                     view.visualRect(view.model().index(2, 0)).center())
    qapp.processEvents()
    assert table.chosen() == [2]


def test_in_single_mode_clicking_a_row_picks_it_and_the_dot_moves(qapp):
    table = _shown(qapp, ChoiceTable(_choice(mode="single", checked=(0,))))
    view = table.table
    QTest.mouseClick(view.viewport(), Qt.LeftButton, Qt.NoModifier,
                     view.visualRect(view.model().index(3, 2)).center())
    qapp.processEvents()
    assert table.chosen() == [3]
    QTest.mouseClick(view.viewport(), Qt.LeftButton, Qt.NoModifier,
                     view.visualRect(view.model().index(1, 0)).center())
    qapp.processEvents()
    assert table.chosen() == [1]


def test_single_mode_opens_with_one_dot_however_many_were_checked(qapp):
    table = _shown(qapp, ChoiceTable(_choice(mode="single", checked=(1, 3))))
    assert table.chosen() == [1]


def test_space_ticks_the_current_row(qapp):
    table = _shown(qapp, ChoiceTable(_choice()))
    table.table.setCurrentCell(4, 1)
    QTest.keyClick(table.table, Qt.Key_Space)
    assert table.chosen() == [4]


def test_what_is_chosen_is_in_the_list_s_order(qapp):
    table = _shown(qapp, ChoiceTable(_choice()))
    for row in (4, 0, 2):
        table.set_checked(row)
    assert table.chosen() == [0, 2, 4]


def test_valid_means_one_in_single_mode_and_at_least_one_in_multiple(qapp):
    many = ChoiceTable(_choice())
    assert not many.valid()
    many.set_checked(0)
    many.set_checked(1)
    assert many.valid()
    one = ChoiceTable(_choice(mode="single"))
    assert not one.valid()
    one.set_checked(2)
    assert one.valid()


def test_the_count_says_what_is_chosen(qapp):
    many = _shown(qapp, ChoiceTable(_choice(checked=(0, 2))))
    assert many.count.text() == "2 of 5 chosen"
    one = _shown(qapp, ChoiceTable(_choice(mode="single")))
    assert one.count.text() == "nothing chosen yet"
    one.set_checked(2)
    assert one.count.text() == "QA-3 chosen"
