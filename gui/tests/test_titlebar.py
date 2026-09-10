"""The window's own title bar, and the frame it stands in for.

Two promises to keep. The head of the window is ours - the mark, the name, the
window controls, in the design's slate - and does everything the desktop's did:
moves, maximizes on a double click, resizes from the edges. And it is only the
head: the menus and everything under them are exactly as they were, moved down
rather than rebuilt.
"""

import os
import sys

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cms_gui import main_window as main_window_mod, theme, titlebar
from cms_gui import version as gui_version
from cms_gui.settings import Settings


@pytest.fixture
def window(qapp, monkeypatch):
    monkeypatch.delenv(titlebar.SYSTEM_FRAME_ENV, raising=False)
    settings = Settings()
    settings.sidebar_collapsed = False
    settings.save_hidden_nav_items([])
    win = main_window_mod.MainWindow()
    win.resize(1200, 800)
    win.show()
    qapp.processEvents()
    yield win
    win.history.clear()
    win.close()


def _send(widget, kind, local, buttons=Qt.LeftButton):
    """A mouse event at ``local`` in ``widget``, with the button held or not."""
    button = Qt.LeftButton if kind in (QEvent.MouseButtonPress,
                                       QEvent.MouseButtonRelease) else Qt.NoButton
    event = QMouseEvent(kind, QPointF(local), QPointF(widget.mapToGlobal(local)),
                        button, buttons, Qt.NoModifier)
    widget.event(event)


def _empty_spot(bar):
    """A point on the bar that is neither a label nor a control."""
    return QPoint(bar.minimize_button.x() - 40, bar.height() // 2)


def _dominant_colour(widget):
    image = widget.grab().toImage()
    counts = {}
    for x in range(image.width()):
        for y in range(image.height()):
            name = image.pixelColor(x, y).name()
            counts[name] = counts.get(name, 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0]


# ------------------------------------------------------------- only the head

def test_the_desktops_frame_is_replaced_by_the_bar(window):
    assert window.windowFlags() & Qt.FramelessWindowHint
    bar = window.frame.bar
    assert bar.isVisible()
    assert bar.geometry().top() == titlebar.EDGE
    assert bar.height() == titlebar.HEIGHT
    assert bar.width() == window.width() - 2 * titlebar.EDGE


def test_the_menus_are_the_windows_own_and_start_under_the_bar(window):
    """Moved down, not rebuilt: the menu bar is still QMainWindow's."""
    menu = window.menuBar()
    assert window.menuWidget() is menu
    assert menu.geometry().top() == window.frame.bar.geometry().bottom() + 1
    assert [a.text().replace("&", "") for a in menu.actions()] == [
        "File", "Run", "View", "Tools", "Help"]


def test_the_bar_says_what_the_tool_is(window):
    bar = window.frame.bar
    assert bar.name.text() == titlebar.NAME
    assert bar.version.text() == gui_version()


def test_the_bar_is_the_brand_slate_in_both_themes(window, qapp):
    try:
        for dark in (False, True):
            theme.set_dark_mode(dark)
            qapp.setStyleSheet(theme.stylesheet())
            qapp.processEvents()
            assert _dominant_colour(window.frame.bar) == theme.TITLEBAR_BG.lower()
    finally:
        theme.set_dark_mode(False)
        qapp.setStyleSheet(theme.stylesheet())


# ------------------------------------------------------ what the frame did

def test_maximize_and_restore_from_the_bar(window, qapp):
    bar = window.frame.bar
    QTest.mouseClick(bar.maximize_button, Qt.LeftButton)
    qapp.processEvents()
    assert window.isMaximized()
    assert bar.maximize_button.kind == "restore"
    # Maximized there is no edge: nothing to resize by, no hairline, no inset.
    assert all(grip.isHidden() for grip in window.frame.grips)
    margins = window.contentsMargins()
    assert (margins.left(), margins.top()) == (0, titlebar.HEIGHT)

    QTest.mouseClick(bar.maximize_button, Qt.LeftButton)
    qapp.processEvents()
    assert not window.isMaximized()
    assert bar.maximize_button.kind == "maximize"
    assert not any(grip.isHidden() for grip in window.frame.grips)


def test_a_double_click_on_the_bar_maximizes(window, qapp):
    bar = window.frame.bar
    QTest.mouseDClick(bar, Qt.LeftButton, pos=_empty_spot(bar))
    qapp.processEvents()
    assert window.isMaximized()


def test_minimize_and_close_do_what_they_say(window, monkeypatch, qapp):
    asked = []
    monkeypatch.setattr(window, "showMinimized", lambda: asked.append(True))
    QTest.mouseClick(window.frame.bar.minimize_button, Qt.LeftButton)
    assert asked == [True]

    QTest.mouseClick(window.frame.bar.close_button, Qt.LeftButton)
    qapp.processEvents()
    assert not window.isVisible()


def test_close_goes_through_the_windows_own_questions(window, monkeypatch):
    """The bar's close is the window's close: a live run is still asked about."""
    monkeypatch.setattr(window.process, "is_running", lambda: True)
    monkeypatch.setattr(window, "_confirm_close_during_run", lambda: False)
    QTest.mouseClick(window.frame.bar.close_button, Qt.LeftButton)
    assert window.isVisible()


def test_dragging_the_bar_moves_the_window(window, qapp):
    """By hand here: offscreen has no window manager to hand the move to."""
    bar = window.frame.bar
    start = window.pos()
    spot = _empty_spot(bar)
    _send(bar, QEvent.MouseButtonPress, spot)
    _send(bar, QEvent.MouseMove, spot + QPoint(60, 30))
    _send(bar, QEvent.MouseButtonRelease, spot + QPoint(60, 30), Qt.NoButton)
    qapp.processEvents()
    assert window.pos() == start + QPoint(60, 30)


def test_a_click_on_the_bar_is_not_a_drag(window, qapp):
    bar = window.frame.bar
    start = window.pos()
    spot = _empty_spot(bar)
    _send(bar, QEvent.MouseButtonPress, spot)
    _send(bar, QEvent.MouseMove, spot + QPoint(1, 0))
    _send(bar, QEvent.MouseButtonRelease, spot + QPoint(1, 0), Qt.NoButton)
    assert window.pos() == start


def test_every_edge_and_corner_has_a_grip_on_it(window):
    width, height = window.width(), window.height()
    grips = {grip.edges: grip for grip in window.frame.grips}
    assert len(grips) == 8
    right = grips[Qt.RightEdge].geometry()
    assert right.right() == width - 1 and right.width() == titlebar.GRIP
    bottom = grips[Qt.BottomEdge].geometry()
    assert bottom.bottom() == height - 1
    corner = grips[Qt.BottomEdge | Qt.RightEdge]
    assert corner.geometry().bottomRight() == QPoint(width - 1, height - 1)
    assert corner.cursor().shape() == Qt.SizeFDiagCursor
    assert grips[Qt.TopEdge | Qt.RightEdge].cursor().shape() == Qt.SizeBDiagCursor
    assert grips[Qt.LeftEdge].cursor().shape() == Qt.SizeHorCursor
    assert grips[Qt.TopEdge].cursor().shape() == Qt.SizeVerCursor


def test_dragging_a_corner_resizes_the_window(window, qapp):
    grip = next(g for g in window.frame.grips
                if g.edges == Qt.BottomEdge | Qt.RightEdge)
    size = window.size()
    _send(grip, QEvent.MouseButtonPress, QPoint(5, 5))
    _send(grip, QEvent.MouseMove, QPoint(45, 25))
    _send(grip, QEvent.MouseButtonRelease, QPoint(45, 25), Qt.NoButton)
    qapp.processEvents()
    assert (window.width(), window.height()) == (size.width() + 40,
                                                 size.height() + 20)


def test_a_resize_never_goes_below_the_windows_minimum(window, qapp):
    grip = next(g for g in window.frame.grips if g.edges == Qt.RightEdge)
    least = window.minimumSizeHint().expandedTo(window.minimumSize())
    _send(grip, QEvent.MouseButtonPress, QPoint(1, 5))
    _send(grip, QEvent.MouseMove, QPoint(-5000, 5))
    _send(grip, QEvent.MouseButtonRelease, QPoint(-5000, 5), Qt.NoButton)
    qapp.processEvents()
    assert window.width() >= least.width()


def test_the_edge_is_drawn_where_the_frame_was(window, qapp):
    image = window.grab().toImage()
    edge = theme.WINDOW_EDGE.lower()
    middle = window.height() // 2
    assert image.pixelColor(0, middle).name() == edge
    assert image.pixelColor(window.width() - 1, middle).name() == edge
    assert image.pixelColor(window.width() // 2, window.height() - 1).name() == edge


# ------------------------------------------------------ the desktop's frame

@pytest.mark.parametrize("value, drawn", [
    ("", True), ("0", True), ("false", True), ("off", True),
    ("1", False), ("yes", False), ("true", False)])
def test_the_desktops_frame_can_be_kept(value, drawn):
    assert titlebar.wanted({titlebar.SYSTEM_FRAME_ENV: value}) is drawn
    assert titlebar.wanted({}) is True


def test_keeping_the_desktops_frame_leaves_the_window_as_it_was(qapp, monkeypatch):
    monkeypatch.setenv(titlebar.SYSTEM_FRAME_ENV, "1")
    win = main_window_mod.MainWindow()
    try:
        assert win.frame is None
        assert not win.windowFlags() & Qt.FramelessWindowHint
        margins = win.contentsMargins()
        assert (margins.left(), margins.top(), margins.right(), margins.bottom()) \
            == (0, 0, 0, 0)
    finally:
        win.history.clear()
        win.close()
