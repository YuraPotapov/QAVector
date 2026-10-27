"""Native Qt delivery through the real menus, pages and settings dialog."""

import pytest

from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest

from cms_gui import main_window
from cms_gui.hotkeys import portable
from cms_gui.settings import Settings


@pytest.fixture
def window(qapp, tmp_path, monkeypatch, dispose):
    settings = Settings()
    settings._qs = QSettings(str(tmp_path / "gui.ini"), QSettings.IniFormat)
    monkeypatch.setattr(main_window, "Settings", lambda: settings)
    monkeypatch.setattr(main_window.MainWindow, "refresh_inventory", lambda self: None)
    made = main_window.MainWindow()
    made.show()
    made.activateWindow()
    qapp.processEvents()
    yield made
    dispose(made)


def test_real_catalogue_includes_menus_components_and_unassigned_actions(window):
    registry = window.action_registry
    entries = {spec.id for spec in registry.specs()}
    assert {"application.search", "application.settings", "application.refresh",
            "credentials.save", "run.start", "navigate.cycles", "cycles.save",
            "find.open"} <= entries
    assert registry.bindings()["application.settings"] == ""
    assert registry.bindings() == registry.defaults()
    assert registry.validate(registry.bindings()) == []
    assert registry.spec("application.search").scope == "application"
    assert registry.spec("application.refresh").scope == "component/main_window"


def test_apply_updates_menu_button_and_dispatches_only_the_new_key(window, qapp):
    calls = []
    window.refresh_action.triggered.connect(lambda: calls.append("refresh"))
    window.show_page("launch")
    field = window.launch.url
    field.setFocus()
    qapp.processEvents()
    QTest.keyClick(field, Qt.Key_F5)
    assert calls == ["refresh"]
    window.action_registry.apply({"application.refresh": "Alt+F9"})
    assert portable(window.refresh_action.shortcut()) == "Alt+F9"
    assert "Alt+F9" in window.refresh_button.toolTip()
    QTest.keyClick(field, Qt.Key_F5)
    QTest.keyClick(field, Qt.Key_F9, Qt.AltModifier)
    assert calls == ["refresh", "refresh"]
    window.refresh_button.click()
    assert calls == ["refresh"] * 3


def test_credential_save_and_cycle_save_use_their_own_pages(window, qapp):
    calls = []
    window.save_users_action.triggered.connect(lambda: calls.append("credentials"))
    window.show_page("cycles")
    window.cycles.setFocus()
    qapp.processEvents()
    QTest.keyClick(window.cycles, Qt.Key_S, Qt.ControlModifier)
    assert calls == []
    window.show_page("credentials")
    window.credentials.setFocus()
    qapp.processEvents()
    QTest.keyClick(window.credentials, Qt.Key_S, Qt.ControlModifier)
    assert calls == ["credentials"]


def test_disabled_action_and_hidden_navigation_do_not_execute(window, qapp):
    stopped = []
    window.stop_action.triggered.connect(lambda: stopped.append(True))
    window.action_registry.apply({"run.stop": "Alt+F10", "navigate.history": "Alt+F11"})
    window.show_page("launch")
    window.launch.url.setFocus()
    qapp.processEvents()
    QTest.keyClick(window.launch.url, Qt.Key_F10, Qt.AltModifier)
    assert stopped == []
    window.set_nav_item_visible("history", False)
    QTest.keyClick(window.launch.url, Qt.Key_F11, Qt.AltModifier)
    assert window._page_key == "launch"


def test_dynamic_navigation_and_stages_hints_follow_assignment(window):
    window.action_registry.apply({"navigate.history": "Alt+F11",
                                  "view.go_to_stages": "Alt+F12"})
    assert "Alt+F11" in window._nav_buttons["history"].toolTip()
    assert "Alt+F12" in window.cycles.output.stages_keys.text()
    window.set_sidebar_collapsed(True)
    assert "Alt+F11" in window._nav_buttons["history"].toolTip()
    assert "Alt+F12" in window.cycles.output.detach_button.toolTip()


def test_run_variants_follow_running_availability(window, monkeypatch):
    asked = []
    monkeypatch.setattr(window, "ask_recording_account", lambda: asked.append(True))
    monkeypatch.setattr(window.process, "is_running", lambda: True)
    window._set_running(True)
    assert not any(action.isEnabled() for action in (
        window.record_action, window.empty_action, window.background_action))
    window.start_recording()
    assert asked == []
    monkeypatch.setattr(window.process, "is_running", lambda: False)
    window._set_running(False)
    assert window.record_action.isEnabled() and window.empty_action.isEnabled()


def test_background_shortcut_availability_follows_form_changes(window, monkeypatch):
    blockers = [("example", "requires a browser")]
    monkeypatch.setattr(window.launch, "background_blockers", lambda: blockers)
    window.show_page("launch")
    window.launch.state_changed.emit()
    assert not window.background_action.isEnabled()
    blockers.clear()
    window.launch.state_changed.emit()
    assert window.background_action.isEnabled()


def _focus_on(window, where):
    """Leave focus where a person may have left it before using the rail."""
    from PySide6.QtWidgets import QApplication

    if where == "page":
        window.show_page("launch")
        window.launch.url.setFocus()
    elif where == "toolbar":
        window.refresh_button.setFocus()       # clicked: a push button takes focus
    else:
        focused = QApplication.focusWidget()
        if focused is not None:
            focused.clearFocus()               # a dialog just closed, the window raised


@pytest.mark.parametrize("where", ["page", "toolbar", "nowhere"])
def test_credentials_keys_work_once_the_rail_has_brought_the_page(window, qapp, where):
    # The keys belong to the page, not to wherever focus was left: a click on
    # the rail does not move it.
    from PySide6.QtWidgets import QApplication

    calls = []
    window.save_users_action.triggered.connect(lambda: calls.append("save"))
    window.reload_users_action.triggered.connect(lambda: calls.append("reload"))
    _focus_on(window, where)
    qapp.processEvents()
    QTest.mouseClick(window._nav_buttons["credentials"], Qt.LeftButton)
    qapp.processEvents()
    target = QApplication.focusWidget() or window
    QTest.keyClick(target, Qt.Key_S, Qt.ControlModifier)
    QTest.keyClick(target, Qt.Key_R, Qt.ControlModifier)
    assert calls == ["save", "reload"]
