"""Capture, conflict decisions and the Apply/Cancel boundary of hotkeys."""

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from cms_gui import widgets
from cms_gui.hotkeyeditor import HotkeyEditor, RECORDING, UNASSIGNED
from cms_gui.hotkeys import ActionRegistry, ActionSpec


class MemorySettings:
    def __init__(self):
        self.saved = {}
        self.fail = False
        self.saves = 0

    def hotkey_overrides(self):
        return dict(self.saved)

    def save_hotkey_overrides(self, values):
        if self.fail:
            raise OSError("Cannot access settings file")
        self.saves += 1
        self.saved = dict(values)


@pytest.fixture
def editor(qapp, dispose):
    host = QWidget()
    store = MemorySettings()
    registry = ActionRegistry(store, host)
    calls = []
    action = registry.bind(ActionSpec("alpha", "Alpha", "First action", "File", "Ctrl+J"),
                           host, lambda: calls.append("alpha"))
    registry.bind(ActionSpec("beta", "Beta", "Second action", "File", "Ctrl+L"),
                  host, lambda: calls.append("beta"))
    registry.register(ActionSpec("empty", "Empty", "Without a default", "File"))
    view = HotkeyEditor(registry)
    column = QVBoxLayout(host)
    column.addWidget(view)
    host.resize(720, 500)
    host.show()
    host.activateWindow()
    QTest.qWaitForWindowActive(host)
    yield view, registry, store, calls, action
    dispose(host)


def select(view, action_id):
    row = next(row for row, spec in enumerate(view._specs) if spec.id == action_id)
    view.table.setCurrentCell(row, 1)
    return row


def test_table_is_read_only_and_includes_unassigned_actions(editor):
    view, registry, *_ = editor
    assert view.table.rowCount() == len(registry.specs()) == 3
    assert view.table.columnCount() == 2
    assert [view.table.horizontalHeaderItem(i).text() for i in range(2)] == ["Action", "Shortcut"]
    for row in range(view.table.rowCount()):
        assert not (view.table.item(row, 0).flags() & Qt.ItemIsEditable)
        assert not (view.table.item(row, 1).flags() & Qt.ItemIsEditable)
    assert view.table.item(select(view, "empty"), 1).text() == UNASSIGNED


def test_capture_changes_only_the_draft_and_does_not_execute_actions(editor):
    view, registry, _store, calls, action = editor
    select(view, "alpha")
    view.start_capture()
    assert registry.suspended
    assert view.table.item(view.table.currentRow(), 1).text() == RECORDING
    QTest.keyClick(view.table, Qt.Key_J, Qt.ControlModifier)
    assert calls == []
    assert not registry.suspended
    assert not view.capture.active
    assert view.draft["alpha"] == "Ctrl+J"
    view.start_capture()
    QTest.keyClick(view.table, Qt.Key_F9, Qt.ControlModifier | Qt.ShiftModifier)
    assert view.draft["alpha"] == "Ctrl+Shift+F9"
    assert registry.bindings()["alpha"] == "Ctrl+J"
    assert action.shortcut().toString() == "Ctrl+J"


def test_modifier_only_and_autorepeat_do_not_finish_capture(editor):
    view, registry, *_ = editor
    select(view, "alpha")
    view.start_capture()
    QTest.keyPress(view.table, Qt.Key_Control)
    QTest.keyRelease(view.table, Qt.Key_Control)
    assert view.capture.active and registry.suspended
    repeat = QKeyEvent(QEvent.KeyPress, Qt.Key_F8, Qt.NoModifier, "", True, 2)
    QApplication.sendEvent(view.table, repeat)
    assert view.capture.active and view.draft["alpha"] == "Ctrl+J"
    QTest.keyClick(view.table, Qt.Key_F8)
    assert view.draft["alpha"] == "F8" and not registry.suspended


def test_a_held_key_is_captured_once_and_swallows_repeat_until_release(editor):
    view, registry, _store, calls, _action = editor
    select(view, "alpha")
    view.start_capture()
    QTest.keyPress(view.table, Qt.Key_J, Qt.ControlModifier)
    for _ in range(3):
        QApplication.sendEvent(view.table, QKeyEvent(
            QEvent.KeyPress, Qt.Key_J, Qt.ControlModifier, "j", True, 1))
    assert view.capture.active and calls == []
    QTest.keyRelease(view.table, Qt.Key_J, Qt.ControlModifier)
    assert not view.capture.active and not registry.suspended and calls == []


@pytest.mark.parametrize("key,expected", [(Qt.Key_Delete, "Ctrl+Del"),
                                          (Qt.Key_Backspace, "Ctrl+Backspace")])
def test_delete_and_backspace_assign_a_key_instead_of_clearing(editor, key, expected):
    view, *_ = editor
    select(view, "alpha")
    view.start_capture()
    QTest.keyClick(view.table, key, Qt.ControlModifier)
    assert view.draft["alpha"] == expected


def test_shift_let_go_first_still_finishes_the_chord(editor):
    # Ctrl+Shift+1 goes down as "!" and, Shift let go first, comes up as "1".
    view, registry, *_ = editor
    select(view, "alpha")
    view.start_capture()
    for kind, key, modifiers in (
            (QEvent.KeyPress, Qt.Key_Exclam, Qt.ControlModifier | Qt.ShiftModifier),
            (QEvent.KeyRelease, Qt.Key_Shift, Qt.ControlModifier),
            (QEvent.KeyRelease, Qt.Key_1, Qt.ControlModifier)):
        QApplication.sendEvent(view.table, QKeyEvent(kind, key, modifiers))
    assert not view.capture.active and not registry.suspended
    assert view.draft["alpha"] == "Ctrl+Shift+!"


@pytest.mark.parametrize("key", [Qt.Key_Tab, Qt.Key_Return, Qt.Key_Down])
def test_a_bare_navigation_key_is_refused_for_the_whole_application(editor, monkeypatch, key):
    view, *_ = editor
    said = []
    monkeypatch.setattr(widgets, "warn", lambda _parent, _title, text, *_rest: said.append(text))
    select(view, "alpha")
    view.start_capture()
    QTest.keyClick(view.table, key)
    assert view.draft["alpha"] == "Ctrl+J"
    assert "navigation or editing key" in said[0]


def test_escape_and_focus_loss_restore_the_previous_cell_and_shortcuts(editor):
    view, registry, *_ = editor
    row = select(view, "alpha")
    view.start_capture()
    QTest.keyClick(view.table, Qt.Key_Escape)
    assert view.table.item(row, 1).text() == "Ctrl+J"
    assert not registry.suspended and not view.capture.active
    view.start_capture()
    view.clear_button.setFocus()
    QApplication.processEvents()
    assert not registry.suspended and not view.capture.active
    assert view.draft["alpha"] == "Ctrl+J"


def test_conflict_names_the_owner_and_reassignment_is_only_in_the_draft(editor, monkeypatch):
    view, registry, *_ = editor
    questions = []
    monkeypatch.setattr(widgets, "choose", lambda *args, **kwargs:
                        questions.append((args, kwargs)) or "Reassign")
    assert view.propose("beta", "Ctrl+J")
    assert "Alpha" in questions[0][0][2]
    assert view.draft["alpha"] == "" and view.draft["beta"] == "Ctrl+J"
    assert registry.bindings()["alpha"] == "Ctrl+J"
    # A later edit sees the pending reassignment, not the active bindings.
    assert view.propose("empty", "Ctrl+J")
    assert "Beta" in questions[1][0][2]
    assert view.draft["beta"] == ""


def test_conflict_cancel_and_retry_keep_previous_assignment(editor, monkeypatch):
    view, registry, *_ = editor
    select(view, "beta")
    monkeypatch.setattr(widgets, "choose", lambda *_args, **_kwargs: "")
    assert not view.propose("beta", "Ctrl+J")
    assert view.draft == registry.bindings()
    monkeypatch.setattr(widgets, "choose", lambda *_args, **_kwargs: "Another combination")
    assert not view.propose("beta", "Ctrl+J")
    assert view.capture.active
    assert view.draft["beta"] == "Ctrl+L"
    view.cancel_capture()


def test_apply_discard_clear_reset_and_reopen(editor):
    view, registry, store, *_ = editor
    select(view, "alpha")
    view.clear_selected()
    assert view.draft["alpha"] == "" and registry.bindings()["alpha"] == "Ctrl+J"
    assert view.apply()
    assert registry.bindings()["alpha"] == "" and store.saved["alpha"] == ""
    assert not view.dirty
    view.reset_selected()
    assert view.draft["alpha"] == "Ctrl+J" and view.dirty
    view.discard()
    assert view.draft["alpha"] == ""
    reopened = HotkeyEditor(registry)
    try:
        assert reopened.draft["alpha"] == ""
        reopened.reset_all()
        assert reopened.draft == registry.defaults()
        assert reopened.apply()
        assert store.saved == {}
    finally:
        reopened.close()
        reopened.deleteLater()


def test_save_failure_keeps_active_keys_and_the_editable_draft(editor, monkeypatch):
    view, registry, store, *_ = editor
    notices = []
    monkeypatch.setattr(widgets, "warn", lambda *args: notices.append(args))
    view.propose("alpha", "Ctrl+F8")
    store.fail = True
    assert not view.apply()
    assert registry.bindings()["alpha"] == "Ctrl+J"
    assert view.draft["alpha"] == "Ctrl+F8" and view.dirty
    assert "Cannot access" in notices[0][2]


def test_hiding_editor_restores_keyboard_delivery(editor):
    view, registry, *_ = editor
    view.start_capture()
    view.hide()
    assert not registry.suspended and not view.capture.active


def test_capture_also_blocks_shortcuts_outside_the_registry(editor):
    from PySide6.QtGui import QKeySequence, QShortcut

    view, _registry, _store, calls, _action = editor
    shortcut = QShortcut(QKeySequence("Ctrl+F7"), view.window())
    shortcut.setContext(Qt.ApplicationShortcut)
    shortcut.activated.connect(lambda: calls.append("external"))
    view.start_capture()
    QTest.keyClick(view.table, Qt.Key_F7, Qt.ControlModifier)
    assert calls == []
    assert view.draft["alpha"] == "Ctrl+F7"


def test_double_click_starts_capture_only_in_hotkey_column(editor):
    view, *_ = editor
    select(view, "alpha")
    view._double_clicked(0, 0)
    assert not view.capture.active
    view._double_clicked(0, 1)
    assert view.capture.active
    view.cancel_capture()


def test_ukrainian_key_code_records_the_same_chord_as_english(editor):
    view, *_ = editor
    select(view, "alpha")
    for key in (ord("R"), ord("\u041a")):
        view.start_capture()
        QApplication.sendEvent(view.table, QKeyEvent(
            QEvent.KeyPress, key, Qt.ControlModifier, "does not define the shortcut"))
        QApplication.sendEvent(view.table, QKeyEvent(
            QEvent.KeyRelease, key, Qt.ControlModifier))
        assert view.draft["alpha"] == "Ctrl+R"


def test_plugin_registered_while_open_appears_without_losing_draft_or_selection(editor):
    view, registry, *_ = editor
    select(view, "beta")
    view.propose("beta", "Ctrl+F8")
    registry.register(ActionSpec("plugin.first", "A plugin action", category="File",
                                 default_shortcut="Ctrl+F7"))
    assert view.table.rowCount() == 4
    assert view._selected_id() == "beta"
    assert view.draft["beta"] == "Ctrl+F8" and view.dirty
    assert view.draft["plugin.first"] == "Ctrl+F7"
    assert view.table.item(select(view, "plugin.first"), 0).text() == "A plugin action"
    view.discard()
    assert view.draft == registry.bindings()
    assert view.draft["beta"] == "Ctrl+L"


def test_new_action_stops_capture_safely_before_resorting_rows(editor):
    view, registry, *_ = editor
    select(view, "beta")
    view.start_capture()
    registry.register(ActionSpec("plugin.first", "A plugin action", category="File"))
    assert not view.capture.active and not registry.suspended
    assert view._selected_id() == "beta"
    assert view.draft["beta"] == "Ctrl+L"


def test_plugin_saved_override_updates_untouched_rows_in_the_open_table(qapp):
    # A plugin absent at startup can bring an earlier user override with it.
    store = MemorySettings()
    store.saved["plugin.saved"] = "Ctrl+J"
    registry = ActionRegistry(store)
    registry.register(ActionSpec("alpha", "Alpha", default_shortcut="Ctrl+J"))
    view = HotkeyEditor(registry)
    try:
        registry.register(ActionSpec("plugin.saved", "A saved plugin action", category="File"))
        assert registry.bindings()["alpha"] == ""
        assert view.draft["alpha"] == "" and not view.dirty
        assert view.draft["plugin.saved"] == "Ctrl+J"
    finally:
        view.close()
        view.deleteLater()


def test_viewport_focus_loss_also_cancels_capture(editor):
    from PySide6.QtGui import QFocusEvent

    view, registry, *_ = editor
    view.start_capture()
    QApplication.sendEvent(view.table.viewport(), QFocusEvent(QEvent.FocusOut))
    assert not view.capture.active and not registry.suspended
    assert view.draft["alpha"] == "Ctrl+J"


def test_conflict_retry_keeps_the_original_action_after_a_selection_change(editor, monkeypatch):
    view, registry, *_ = editor
    select(view, "beta")

    def retry(*_args, **_kwargs):
        registry.register(ActionSpec("plugin.late", "A late plugin action", category="File"))
        select(view, "empty")
        return "Another combination"

    monkeypatch.setattr(widgets, "choose", retry)
    assert not view.propose("beta", "Ctrl+J")
    assert view._selected_id() == "beta" and view.capture.active
    QTest.keyClick(view.table, Qt.Key_F9, Qt.ControlModifier)
    assert view.draft["beta"] == "Ctrl+F9"
    assert view.draft["empty"] == ""


def test_a_row_explains_the_action_and_where_its_key_works(editor):
    view, *_ = editor
    row = select(view, "alpha")
    assert view.table.item(row, 0).toolTip() == "First action\nWorks anywhere in QAVector"
