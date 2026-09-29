"""Stages in a window of its own: what it holds, and how it goes."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

from cms_gui import stageswindow
from cms_gui.settings import Settings


@pytest.fixture
def window(qapp, dispose):
    made = stageswindow.StagesWindow(Settings())
    yield made
    dispose(made)


def test_it_never_keeps_the_application_running(window):
    """Closing the main window must end the process with this one open."""
    assert not window.testAttribute(Qt.WA_QuitOnClose)


def test_it_has_no_parent_so_it_can_go_on_another_screen(window):
    assert window.parent() is None
    assert window.isWindow()


def test_it_holds_the_very_panel_it_is_given_and_gives_it_back(window):
    panel = QLabel("stages")
    window.hold(panel)
    assert panel.window() is window
    assert window.panel() is panel
    assert window.release() is panel
    assert window.panel() is None
    assert window.release() is None
    panel.deleteLater()


def test_a_person_closing_it_says_so(window):
    said = []
    window.closed_by_person.connect(lambda: said.append(True))
    window.show()
    window.close()
    assert said == [True]
    assert window.closed()


def test_the_application_shutting_it_is_not_a_person_closing_it(window):
    said = []
    window.closed_by_person.connect(lambda: said.append(True))
    window.show()
    window.shut()
    assert said == []
    assert window.closed()


def test_where_it_was_is_kept_for_next_time(qapp, dispose):
    settings = Settings()
    first = stageswindow.StagesWindow(settings)
    first.resize(610, 470)
    first.show()
    first.shut()
    dispose(first)
    second = stageswindow.StagesWindow(settings)
    try:
        assert (second.width(), second.height()) == (610, 470)
    finally:
        dispose(second)


def test_bringing_it_forward_takes_it_off_the_taskbar_and_keeps_maximized(window):
    window.show()
    window.setWindowState(Qt.WindowMaximized | Qt.WindowMinimized)
    window.bring_forward()
    assert not window.windowState() & Qt.WindowMinimized
    assert window.windowState() & Qt.WindowMaximized
    assert window.isVisible()


def test_its_title_names_the_cycle_the_run_is_of(window):
    window.set_run_name("Development")
    assert window.windowTitle() == "Stages - Development"
    if window.frame is not None:
        assert window.frame.bar.name.text() == "Stages - Development"
    window.set_run_name("")
    assert window.windowTitle() == "Stages"
