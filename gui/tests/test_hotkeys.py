"""Assignments are drafts until saved, and Qt remains the dispatch authority."""

import pytest

from PySide6.QtCore import QEvent, QSettings, Qt
from PySide6.QtGui import QAction, QKeyEvent, QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLineEdit, QPushButton, QVBoxLayout, QWidget

from cms_gui.hotkeys import (ActionRegistry, ActionSpec, bind_action,
                             bind_tooltip, portable, scopes_overlap,
                             sequence_from_event)
from cms_gui.settings import Settings


@pytest.fixture
def settings(tmp_path):
    value = Settings()
    value._qs = QSettings(str(tmp_path / "gui.ini"), QSettings.IniFormat)
    return value


def spec(action_id, shortcut="", scope="application"):
    return ActionSpec(action_id, action_id.title(), default_shortcut=shortcut,
                      scope=scope)


def test_conflicts_include_draft_and_application_descendants(qapp, settings):
    registry = ActionRegistry(settings)
    for entry in (spec("global", "Ctrl+K"), spec("one", "Ctrl+S", "page/one"),
                  spec("two", "Ctrl+S", "page/two"),
                  spec("child", "", "page/one/editor")):
        registry.register(entry)
    assert registry.validate(registry.bindings()) == []
    assert registry.conflicts("one", "Ctrl+S") == []
    assert [entry.id for entry in registry.conflicts("one", "Ctrl+K")] == ["global"]
    draft = registry.bindings()
    draft["one"] = "Alt+R"
    assert [entry.id for entry in registry.conflicts("child", "Alt+R", draft)] == ["one"]
    assert registry.conflicts("one", "Alt+R", draft) == []
    assert scopes_overlap("component/reader", "page/one")


def test_unknown_plugin_and_explicit_empty_survive_restart(qapp, settings):
    settings.save_hotkey_overrides({"plugin.offline": "Alt+F9"})
    registry = ActionRegistry(settings)
    registry.register(spec("run", "Ctrl+R"))
    registry.apply({"run": ""})
    assert settings.hotkey_overrides() == {"plugin.offline": "Alt+F9", "run": ""}
    reloaded = ActionRegistry(settings)
    reloaded.register(spec("run", "Ctrl+R"))
    assert reloaded.bindings()["run"] == ""
    reloaded.register(spec("plugin.offline"))
    assert reloaded.bindings()["plugin.offline"] == "Alt+F9"
    reloaded.apply(reloaded.defaults())
    assert reloaded.bindings()["run"] == "Ctrl+R"
    assert "run" not in settings.hotkey_overrides()


def test_unapplied_values_do_not_affect_actions_and_save_failure_keeps_live_keys(
        qapp, settings, monkeypatch):
    registry = ActionRegistry(settings)
    host = QWidget()
    action = registry.bind(spec("run", "Ctrl+R"), host, lambda: None)
    draft = registry.bindings()
    draft["run"] = "Alt+F9"
    assert portable(action.shortcut()) == "Ctrl+R"

    def fail(_values):
        raise OSError("disk is full")

    monkeypatch.setattr(settings, "save_hotkey_overrides", fail)
    with pytest.raises(OSError):
        registry.apply(draft)
    assert portable(action.shortcut()) == "Ctrl+R"
    assert registry.bindings()["run"] == "Ctrl+R"
    assert settings.hotkey_overrides() == {}


def test_validation_is_atomic_and_does_not_clear_a_conflicting_action(qapp, settings):
    registry = ActionRegistry(settings)
    registry.register(spec("one", "Ctrl+R"))
    registry.register(spec("two", "Ctrl+S"))
    with pytest.raises(ValueError, match="One / Two"):
        registry.apply({"two": "Ctrl+R"})
    assert registry.bindings() == {"one": "Ctrl+R", "two": "Ctrl+S"}
    registry.apply({"one": "", "two": "Ctrl+R"})
    assert registry.bindings() == {"one": "", "two": "Ctrl+R"}


def test_single_chord_validation_and_text_key_protection(qapp, settings):
    registry = ActionRegistry(settings)
    registry.register(spec("global"))
    registry.register(spec("component", "", "component/editor"))
    registry.register(spec("main_window", "", "component/main_window"))
    with pytest.raises(ValueError, match="one key combination"):
        registry.apply({"global": "Ctrl+K, Ctrl+C"})
    with pytest.raises(ValueError, match="navigation or editing key"):
        registry.apply({"global": "R"})
    with pytest.raises(ValueError, match="navigation or editing key"):
        registry.apply({"main_window": "R"})
    registry.apply({"component": "R"})
    assert registry.bindings()["component"] == "R"


def test_broken_or_conflicting_saved_entries_do_not_prevent_registration(qapp, settings):
    settings.save_hotkey_overrides({"broken": "Invalid key name", "one": "Alt+F9",
                                     "two": "Alt+F9", "plugin.missing": "Ctrl+P"})
    registry = ActionRegistry(settings)
    for entry in (spec("two"), spec("broken", "Ctrl+R"), spec("one")):
        registry.register(entry)
    assert registry.bindings() == {"broken": "", "one": "Alt+F9", "two": ""}
    assert registry.validate(registry.bindings()) == []
    assert settings.hotkey_overrides()["two"] == "Alt+F9"
    assert settings.hotkey_overrides()["plugin.missing"] == "Ctrl+P"


def test_nested_capture_suspension_restores_only_shortcuts(qapp, settings):
    registry = ActionRegistry(settings)
    host = QWidget()
    action = registry.bind(spec("run", "Ctrl+R"), host, lambda: None)
    registry.suspend()
    registry.suspend()
    assert registry.suspended and action.shortcut().isEmpty() and action.isEnabled()
    registry.resume()
    assert registry.suspended and action.shortcut().isEmpty()
    registry.resume()
    assert not registry.suspended and portable(action.shortcut()) == "Ctrl+R"


def test_menu_action_tooltip_and_button_follow_applied_assignment(qapp, settings):
    registry = ActionRegistry(settings)
    host = QWidget()
    action = QAction("Run", host)
    action.setToolTip("Run selected configuration")
    registry.register(spec("run", "Ctrl+R"), action)
    button = QPushButton(host)
    bind_tooltip(action, button, "Run selected configuration")
    registry.apply({"run": "Alt+F9"})
    assert "Alt+F9" in action.toolTip() and "Alt+F9" in button.toolTip()
    registry.apply({"run": ""})
    assert action.toolTip() == button.toolTip() == "Run selected configuration"


def test_parentless_components_adopt_the_window_registry(qapp, settings):
    settings.save_hotkey_overrides({"component": "Alt+F9"})
    page = QWidget()
    host = QWidget(page)
    action = bind_action(host, spec("component", "F3", "component/reader"), lambda: None)
    assert portable(action.shortcut()) == "F3"
    window = QWidget()
    window.action_registry = ActionRegistry(settings, window)
    page.setParent(window)
    assert portable(action.shortcut()) == "Alt+F9"
    assert window.action_registry.bindings() == {"component": "Alt+F9"}


def test_qt_dispatch_once_availability_and_new_key(qapp, settings):
    window = QWidget()
    layout = QVBoxLayout(window)
    field = QLineEdit()
    layout.addWidget(field)
    calls = []
    registry = ActionRegistry(settings, window)
    action = registry.bind(spec("run", "Ctrl+R"), window, lambda: calls.append(1))
    window.show()
    window.activateWindow()
    field.setFocus()
    qapp.processEvents()
    QTest.keyClick(field, Qt.Key_R, Qt.ControlModifier)
    assert calls == [1]
    registry.apply({"run": "Alt+F9"})
    QTest.keyClick(field, Qt.Key_R, Qt.ControlModifier)
    assert calls == [1]
    QTest.keyClick(field, Qt.Key_F9, Qt.AltModifier)
    assert calls == [1, 1]
    action.setEnabled(False)
    QTest.keyClick(field, Qt.Key_F9, Qt.AltModifier)
    assert calls == [1, 1]
    window.close()


@pytest.mark.parametrize("english, ukrainian", [(Qt.Key_R, "\u041a"), (Qt.Key_S, "\u0406"),
                                                (Qt.Key_K, "\u041b"), (Qt.Key_G, "\u041f")])
def test_recording_uses_key_codes_consistently_across_layouts(english, ukrainian):
    en = QKeyEvent(QEvent.KeyPress, english, Qt.ControlModifier, "unrelated")
    uk = QKeyEvent(QEvent.KeyPress, ord(ukrainian), Qt.ControlModifier, "unrelated")
    assert sequence_from_event(en) == sequence_from_event(uk)


def test_modifier_only_does_not_complete_recording():
    event = QKeyEvent(QEvent.KeyPress, Qt.Key_Control, Qt.ControlModifier)
    assert sequence_from_event(event) == ""


def test_cyrillic_saved_chords_are_canonicalized_for_conflicts(qapp, settings):
    registry = ActionRegistry(settings)
    registry.register(spec("one", "Ctrl+R"))
    registry.register(spec("two"))
    assert [entry.id for entry in registry.conflicts("two", "Ctrl+\u041a")] == ["one"]


def test_failed_qsettings_write_restores_previous_cache(qapp, settings, monkeypatch):
    settings.save_hotkey_overrides({"one": "Ctrl+R"})
    original = settings._qs

    class FailingSettings:
        def __getattr__(self, name):
            return getattr(original, name)

        def status(self):
            return QSettings.AccessError

    settings._qs = FailingSettings()
    with pytest.raises(OSError, match="Could not save hotkeys"):
        settings.save_hotkey_overrides({"one": "Ctrl+S"})
    assert settings.hotkey_overrides() == {"one": "Ctrl+R"}


def test_invalid_values_in_full_draft_report_validation_errors(qapp, settings):
    registry = ActionRegistry(settings)
    registry.register(spec("one", "Ctrl+R"))
    registry.register(spec("two"))
    with pytest.raises(ValueError, match="Shortcut must be a key combination"):
        registry.apply({"two": ["Ctrl+R"]})
    assert registry.bindings() == {"one": "Ctrl+R", "two": ""}


def test_registering_a_qaction_under_another_id_is_rejected(qapp, settings):
    registry = ActionRegistry(settings)
    host = QWidget()
    action = registry.bind(spec("one", "Ctrl+R"), host, lambda: None)
    with pytest.raises(ValueError, match="already registered as one"):
        registry.register(spec("two", "Ctrl+S"), action)
    assert registry.bindings() == {"one": "Ctrl+R"}


@pytest.mark.parametrize("attached", [False, True])
def test_binding_a_logical_action_twice_on_one_host_reuses_qaction(qapp, settings, attached):
    host = QWidget()
    if attached:
        host.action_registry = ActionRegistry(settings, host)
    calls = []
    entry = spec("component", "F3", "component/reader")
    first = bind_action(host, entry, lambda: calls.append(1))
    second = bind_action(host, entry, lambda: calls.append(2))
    assert second is first
    first.trigger()
    assert calls == [1]


def test_late_plugin_registration_notifies_existing_binding_changes(qapp, settings):
    settings.save_hotkey_overrides({"plugin": "Ctrl+R"})
    registry = ActionRegistry(settings)
    registry.register(spec("run", "Ctrl+R"))
    updates = []
    registry.changed.connect(lambda: updates.append(registry.bindings()))
    registry.register(spec("plugin"))
    assert updates == [{"plugin": "Ctrl+R", "run": ""}]


@pytest.mark.parametrize("sequence, scope, refused", [
    ("Tab", "application", True), ("Return", "application", True),
    ("Down", "component/main_window", True), ("Backspace", "page/one", True),
    ("F5", "application", False), ("Shift+F3", "application", False),
    ("Undo", "page/one", False),
    ("Down", "component/reader", False), ("Ctrl+Return", "application", False)])
def test_widget_keys_never_go_bare_outside_a_component(qapp, settings, sequence, scope, refused):
    registry = ActionRegistry(settings)
    registry.register(spec("one", scope=scope))
    assert bool(registry.validate({"one": sequence})) is refused


class _Platform:
    def __init__(self, name):
        self.name = name

    def platformName(self):                     # noqa: N802 - Qt's spelling
        return self.name


@pytest.mark.parametrize("platform, scan", [("xcb", 11), ("wayland", 11), ("windows", 3)])
def test_the_number_row_is_named_by_where_the_key_is_in_any_layout(monkeypatch, platform, scan):
    import cms_gui.hotkeys as hotkeys

    monkeypatch.setattr(hotkeys, "QGuiApplication", _Platform(platform))
    shifted = Qt.ControlModifier | Qt.ShiftModifier
    # The key right of 1: "@" in English, '"' in Ukrainian - Ctrl+Shift+2 in both.
    english = QKeyEvent(QEvent.KeyPress, Qt.Key_At, shifted, scan, 0, 0, "@")
    ukrainian = QKeyEvent(QEvent.KeyPress, Qt.Key_QuoteDbl, shifted, scan, 0, 0, '"')
    assert sequence_from_event(english) == sequence_from_event(ukrainian) == "Ctrl+Shift+2"


def test_the_slash_key_stays_a_slash_in_ukrainian(monkeypatch):
    # On the Ukrainian layout it types ".", which would be Stop's Ctrl+.
    import cms_gui.hotkeys as hotkeys

    monkeypatch.setattr(hotkeys, "QGuiApplication", _Platform("xcb"))
    event = QKeyEvent(QEvent.KeyPress, Qt.Key_Period, Qt.ControlModifier, 61, 0, 0, ".")
    assert sequence_from_event(event) == "Ctrl+/"


def test_a_platform_alternative_reads_as_which_key_it_is():
    from cms_gui.hotkeys import alternate_name

    assert [alternate_name("Next match", index) for index in range(3)] == [
        "Next match", "Next match, second key", "Next match, third key"]


@pytest.mark.parametrize("scope, said", [
    ("application", "Works anywhere in QAVector"),
    ("component/main_window", "Works in the main window"),
    ("page/credentials", "Works on the Credentials page"),
    ("component/reader", "Works in the focused reader")])
def test_where_a_key_works_is_said_in_words(scope, said):
    from cms_gui.hotkeys import scope_label

    assert scope_label(scope) == said
