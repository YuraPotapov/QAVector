"""The Environments page: the one row that holds a real control, and its wording.

Everything else on this page is text in a cell, which a row of any height can
draw. The URL override is an actual input sitting in a table, which is the one
arrangement where the design's own metrics and the view's own metrics have to be
made to agree.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QLabel

from cms_gui.pages.environments import EnvironmentsPage
from cms_gui.settings import Settings

URL_OVERRIDE = 4          # the column the editor lives in


class FakeInventory:
    envs = [
        {"alias": "local", "value": "localhost:8069",
         "origin": "http://localhost:8069", "count": 11},
        {"alias": "staging", "value": "https://staging.example.invalid/",
         "origin": "https://staging.example.invalid", "count": 10},
        {"alias": "demo", "value": "https://demo.example.invalid/",
         "origin": "", "count": 10},
    ]

    def dirs(self):
        return {}


@pytest.fixture
def page(qapp):
    widget = EnvironmentsPage(Settings())
    widget.resize(1100, 620)
    widget.set_inventory(FakeInventory())
    widget.show()
    qapp.processEvents()
    yield widget
    widget.close()


def _labels(widget):
    return "\n".join(label.text() for label in widget.findChildren(QLabel))


# ------------------------------------------------------------- the odd row out

def test_the_url_override_sits_squarely_inside_its_row(page):
    """It used to hang across the gridline below it.

    The design's inputs are 30px and a row of text is 30px, but a view insets a
    cell widget by the item padding on both sides - so the editor was handed 24,
    rendered at its own minimum anyway, and the extra six went downwards: 3px of
    air above it and 3px of it lying over the divider. Visibly lopsided, and only
    on this one column, which is what made it look like a mistake in the column
    rather than in the row.
    """
    table = page.table
    assert table.rowCount() == len(FakeInventory.envs)
    for row in range(table.rowCount()):
        editor = table.cellWidget(row, URL_OVERRIDE)
        top = table.rowViewportPosition(row)
        above = editor.y() - top
        below = (top + table.rowHeight(row)) - (editor.y() + editor.height())
        assert above == below, ("row %d: %dpx above, %dpx below"
                                % (row, above, below))
        assert above >= 0, "row %d starts above its own row" % row


def test_the_row_is_tall_enough_for_the_editor_rather_than_the_other_way_round(page):
    """The input keeps the height every other input in the app has."""
    table = page.table
    for row in range(table.rowCount()):
        editor = table.cellWidget(row, URL_OVERRIDE)
        assert editor.height() >= editor.minimumSizeHint().height(), row


def test_an_empty_inventory_leaves_the_table_alone(page):
    """No rows, nothing to measure, and no exception on the way through."""
    class Empty:
        envs = []

        def dirs(self):
            return {}

    page.set_inventory(Empty())
    assert page.table.rowCount() == 0


# ------------------------------------------------------------------- wording

def test_the_page_names_no_flag_until_developer_mode(page):
    """A flag is the answer to a question a regular user is not asking."""
    page.set_developer_mode(False)
    plain = _labels(page)
    assert "--" not in plain
    # And it still says what the controls do.
    assert "Flows" in plain and "Reports" in plain and "Sessions" in plain

    page.set_developer_mode(True)
    spelled_out = _labels(page)
    for flag in ("--describe", "--flows-dir", "--reports-dir", "--sessions-dir"):
        assert flag in spelled_out, flag


def test_the_wording_goes_back_when_the_mode_does(page):
    page.set_developer_mode(True)
    page.set_developer_mode(False)
    assert "--" not in _labels(page)


# ------------------------------------------------------------------ search (Ctrl+K)
def test_search_lists_each_environment_and_reveal_selects_it(page):
    hits = page.search_rows()
    assert [h.title for h in hits] == ["local", "staging", "demo"]
    assert hits[1].detail == "https://staging.example.invalid · 10 accounts"
    assert page.show_found("demo")
    assert [index.row() for index in page.table.selectionModel().selectedRows()] == [2]
    assert not page.show_found("gone")
