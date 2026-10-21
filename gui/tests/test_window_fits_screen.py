"""The main window fits a laptop screen, whatever the data in it is called.

A window whose minimum is bigger than the screen's work area cannot be
maximized: GNOME puts it in the corner and the rest runs off the edge. It has
happened twice, both times from data rather than layout - a cycle's name in the
Cycles header, then a session's name in the Log page's picker - and both times
from a page that was not even on screen, because a stacked widget is as wide
as its widest page. These tests build the whole window, so a new page or a new
row is measured without anyone having to remember it.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QComboBox, QStackedWidget

from cms_gui import main_window as main_window_mod

#: A 1366 x 768 laptop, less GNOME's side dock and top bar and some room to
#: spare. Its work area is 1324 x 741.
SCREEN_WIDTH = 1280
SCREEN_HEIGHT = 700

LONG = "shop.example.invalid-" + "a-very-long-name-" * 8


@pytest.fixture
def build(qapp, dispose):
    """``build(before_show)`` - a main window, changed before its first show.

    Before, because that is when a combo sizes itself to its items, and when
    the data a window opens with is already in it.
    """
    windows = []

    def make(before_show=None):
        win = main_window_mod.MainWindow()
        if before_show:
            before_show(win)
        win.show()
        qapp.processEvents()
        windows.append(win)
        return win

    yield make
    for win in windows:
        win.history.clear()
        dispose(win)


def _page_of(widget):
    """The page a widget is on, by class name - for a failure that says where."""
    while widget is not None and not isinstance(widget.parentWidget(), QStackedWidget):
        widget = widget.parentWidget()
    return type(widget).__name__ if widget is not None else "window"


def test_the_window_fits_a_laptop_screen(build):
    win = build()
    hint = win.minimumSizeHint()
    assert hint.width() <= SCREEN_WIDTH
    assert hint.height() <= SCREEN_HEIGHT


def test_every_picker_is_capped(build):
    """A combo is as wide as its longest item unless something says otherwise,
    and most of them list names that come from the person's data."""
    win = build()
    uncapped = sorted("%s: %r" % (_page_of(combo), combo.itemText(0))
                      for combo in win.findChildren(QComboBox)
                      if combo.maximumWidth() > SCREEN_WIDTH // 2)
    assert uncapped == [], "use widgets.capped() for these"


def test_long_names_everywhere_do_not_widen_the_window(build):
    """Every picker and the Cycles header given a name no screen could show."""
    def long_names(win):
        for combo in win.findChildren(QComboBox):
            combo.addItem(LONG)
            combo.setCurrentIndex(combo.count() - 1)
        win.cycles.title.setText(LONG)
        win.cycles.state.setText(LONG)

    assert build(long_names).minimumSizeHint().width() <= SCREEN_WIDTH
