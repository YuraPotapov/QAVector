"""Quick Agent uses the real GUI lifecycle with a fake core and fake turns."""

import pytest

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QVBoxLayout, QWidget

from cms_gui.pages.agents import AgentsView
from cms_gui.quickagent import QuickAgentPopup, RECENT_LIMIT
from cms_gui.settings import Settings
from test_agentchat import FakeTurn
from test_agents_page import FakeCore, _connection, _session


@pytest.fixture
def make(qapp, tmp_path, dispose):
    windows = []
    FakeTurn.made = []

    def build(core=None, memory=None):
        settings = Settings()
        settings._qs = QSettings(str(tmp_path / "quick.ini"), QSettings.IniFormat)
        settings.save_integrations_state({"agents": dict(
            {"folders": [str(tmp_path)], "connection": "claude"}, **(memory or {}))})
        window = QWidget()
        window.resize(1200, 800)
        field = QLineEdit(window)
        QVBoxLayout(window).addWidget(field)
        agents = AgentsView(settings, window)
        agents.hide()
        agents.set_core(core or FakeCore())
        agents.chat.turn_factory = FakeTurn
        popup = QuickAgentPopup(agents, lambda: "Ctrl+Shift+A", window)
        agents.quick_composer = popup
        popup.chat_wanted.connect(lambda sid: agents.open_session(sid) if sid else None)
        window.show()
        window.activateWindow()
        field.setFocus()
        qapp.processEvents()
        popup.open_over(window)
        qapp.processEvents()
        windows.append(window)
        return popup, agents, field

    yield build
    for window in windows:
        for agents in window.findChildren(AgentsView):
            agents.shutdown()
        dispose(window)


def test_keyboard_only_creates_one_session_and_full_chat_owns_the_turn(make, qapp):
    popup, agents, _ = make()
    assert QApplication.focusWidget() is popup.composer.text
    assert agents.chat.current is None
    assert not FakeTurn.made
    assert all(args[0] in ("list", "kinds", "sessions") for args, _ in agents.core.calls)
    QTest.keyClicks(popup.composer.text, "Review the tests")
    QTest.keyClick(popup.composer.text, Qt.Key_Return)
    QTest.keyClick(popup.composer.text, Qt.Key_Return)
    assert len(FakeTurn.made) == 1
    turn = FakeTurn.made[0]
    assert turn.document["message"] == "Review the tests"
    assert agents.chat.turns[turn.session_id] is turn
    assert not popup.isVisible()
    popup.open_over(popup.parentWidget())
    assert popup.composer._session is None
    assert not popup.composer.draft()
    popup.dismiss()
    assert turn.started and not turn.stopped


def test_shift_enter_ime_escape_and_draft_focus_restoration(make, qapp):
    popup, agents, field = make()
    edit = popup.composer.text
    QTest.keyClicks(edit, "draft")
    QTest.keyClick(edit, Qt.Key_Return, Qt.ShiftModifier)
    assert edit.toPlainText() == "draft\n"
    QApplication.sendEvent(edit, QInputMethodEvent("composing", []))
    QTest.keyClick(edit, Qt.Key_Return)
    assert not FakeTurn.made
    QApplication.sendEvent(edit, QInputMethodEvent("", []))
    QTest.keyClick(edit, Qt.Key_Escape)
    qapp.processEvents()
    assert not popup.isVisible()
    assert QApplication.focusWidget() is field
    assert agents.has_unsaved()
    popup.open_over(popup.parentWidget())
    assert "draft" in popup.composer.draft()
    qapp.processEvents()
    assert QApplication.focusWidget() is edit


def test_provider_switch_resets_model_effort_preserves_text_and_default(make, qapp):
    core = FakeCore(connections=[_connection(), _connection("other", "Other", kind="other")])
    core.kinds = list(core.kinds) + [{"kind": "other", "found": "/bin/other", "modes": ["read"],
                                    "efforts": ["low"]}]
    popup, agents, _ = make(core)
    composer = popup.composer
    composer.set_draft("Keep this")
    composer.model.set_value("old-model")
    composer.effort.setCurrentIndex(composer.effort.findData("max"))
    composer.connection.setCurrentIndex(1)
    composer.connection.activated.emit(1)
    assert composer.model.value() == ""
    assert composer.effort.currentData() == ""
    assert composer.effort.findData("max") == -1
    assert composer.draft() == "Keep this"
    assert QApplication.focusWidget() is composer.text
    composer.model.set_value("other-model")
    composer.effort.setCurrentIndex(composer.effort.findData("low"))
    composer.send()
    new = next(doc for args, doc in core.calls if args[0] == "session-new")
    assert new["connection"] == "other"
    assert new["model"] == "other-model" and new["effort"] == "low"
    assert FakeTurn.made[-1].document["model"] == "other-model"
    assert FakeTurn.made[-1].document["effort"] == "low"
    assert agents.chat._memory()["connection"] == "claude"


def test_pending_creation_deduplicates_and_failure_keeps_draft_for_retry(make):
    popup, agents, _ = make()
    original = agents.ask
    requests = []

    def ask(kind, work, done):
        if kind == "session-new":
            requests.append((work, done))
        else:
            original(kind, work, done)

    agents.ask = ask
    popup.composer.set_draft("Try this")
    popup.composer.send()
    popup.submit("Try this")
    popup.dismiss()
    popup.open_over(popup.parentWidget())
    popup.submit("Try this")
    assert len(requests) == 1
    requests[0][1]({"ok": False, "error": "Folder unavailable"})
    assert popup.composer.draft() == "Try this"
    assert popup.composer.note.text() == "Folder unavailable"
    popup.composer.send()
    assert len(requests) == 2
    work, done = requests[1]
    done(work(agents.core, None))
    assert len(FakeTurn.made) == 1


@pytest.mark.parametrize("input_method", ["keyboard", "mouse"])
def test_recent_sessions_sorted_across_connections_keep_their_folder(make, input_method):
    sessions = [_session(str(index), "Task %d" % index, updated=index,
                         folder="/other/project", connection="other") for index in range(8)]
    popup, agents, _ = make(FakeCore(sessions=sessions))
    assert popup.recent.count() == RECENT_LIMIT
    assert popup.recent.item(0).data(Qt.UserRole) == "7"
    popup.recent.setCurrentRow(0)
    row = popup.recent.itemWidget(popup.recent.item(0))
    assert row.title.text() == "Task 7"
    popup._fill_recent()
    assert popup.recent.itemWidget(popup.recent.item(0)) is row
    if input_method == "keyboard":
        popup.recent.setFocus()
        QTest.keyClick(popup.recent, Qt.Key_Return)
    else:
        QTest.mouseClick(row.title, Qt.LeftButton)
    assert agents.chat.current["id"] == "7"
    assert agents.chat.composer.settings()["folder"] == "/other/project"
    assert not FakeTurn.made
    assert not any(args[0] == "session-new" for args, _ in agents.core.calls)


def test_initial_session_list_cannot_override_an_explicit_recent_selection(make):
    remembered, chosen = _session("remembered"), _session("chosen")
    popup, agents, _ = make(FakeCore(sessions=[remembered, chosen]), memory={"session": "remembered"})
    pending = []
    agents.ask = lambda kind, work, done: pending.append((kind, work, done))
    agents.chat.load_sessions()
    popup._open_chat("chosen")
    assert [kind for kind, _, _ in pending] == ["sessions", "session"]
    _, work, done = pending[0]
    done(work(agents.core, None))
    assert len(pending) == 2
    _, work, done = pending[1]
    done(work(agents.core, None))
    assert agents.chat.current["id"] == "chosen"


def test_no_connections_keeps_editor_and_draft_through_settings(make):
    popup, agents, _ = make(FakeCore(connections=[]))
    assert QApplication.focusWidget() is popup.composer.text
    popup.composer.set_draft("After setup")
    assert not popup.composer.send_button.isEnabled()
    popup.composer.send()
    assert not FakeTurn.made
    popup.dismiss()
    assert popup.composer.draft() == "After setup"
    agents.core.connections = [_connection()]
    popup.open_over(popup.parentWidget())
    assert popup.composer.draft() == "After setup"
    assert popup.composer.send_button.isEnabled()


def test_missing_folder_and_unavailable_provider_do_not_create(make, tmp_path):
    popup, agents, _ = make()
    popup.composer.set_draft("Keep me")
    popup.composer.folder.setEditText(str(tmp_path / "missing"))
    popup.composer.send()
    assert "existing folder" in popup.composer.note.text()
    popup.composer.folder.setEditText(str(tmp_path))
    popup.composer.set_kinds([dict(agents.core.kinds[0], found="")])
    popup.composer.send()
    assert "unavailable" in popup.composer.note.text()
    assert not any(args[0] == "session-new" for args, _ in agents.core.calls)
    assert popup.composer.draft() == "Keep me"


def test_shared_chat_draft_and_restrictions_survive_quick_launch(make):
    popup, agents, _ = make()
    agents.chat.composer.set_draft("Unsent full chat")
    popup.composer.set_restrictions({"network": "off"})
    popup.composer.set_draft("Quick task")
    popup.composer.send()
    new = next(doc for args, doc in agents.core.calls if args[0] == "session-new")
    assert new["restrictions"] == {"network": "off"}
    agents.chat.new_conversation()
    assert agents.chat.composer.draft() == "Unsent full chat"


def test_project_reset_discards_quick_draft(make):
    popup, agents, _ = make()
    popup.composer.set_draft("Old project")
    agents.set_core(FakeCore())
    assert not popup.isVisible()
    assert not popup.has_draft()


def test_remembered_connection_defaults_and_missing_connection_fallback(make):
    core = FakeCore(connections=[_connection(), _connection("second", "Second", model="sonnet", effort="high")])
    popup, agents, _ = make(core, memory={"connection": "second"})
    assert popup.composer.connection.currentData() == "second"
    assert popup.composer.model.currentText() == "sonnet"
    assert popup.composer.effort.currentText() == "high"
    popup.composer.set_draft("Keep my text")
    popup.composer.model.set_value("removed-model")
    popup.composer.effort.setCurrentIndex(popup.composer.effort.findData("max"))
    popup.dismiss()
    core.connections = [_connection()]
    popup.open_over(popup.parentWidget())
    assert popup.composer.connection.currentData() == "claude"
    assert popup.composer.model.value() == ""
    assert popup.composer.effort.currentData() == ""
    assert agents.chat._memory()["connection"] == "second"


def test_failed_connection_read_is_shown_and_can_be_retried(make):
    core = FakeCore(**{"list": {"ok": False, "error": "Cannot read Connections"}})
    popup, _, _ = make(core)
    assert "Cannot read Connections" in popup.composer.note.text()
    popup.composer.set_draft("Keep me")
    assert not popup.composer.send_button.isEnabled()
    popup.dismiss()
    del core.answers["list"]
    popup.open_over(popup.parentWidget())
    assert popup.composer.draft() == "Keep me"
    assert popup.composer.send_button.isEnabled()


def test_first_turn_refusal_returns_message_to_shared_chat_for_retry(make):
    popup, agents, _ = make()
    popup.composer.set_draft("Retry this in Chat")
    popup.composer.send()
    FakeTurn.made[-1].refuse("unavailable", "CLI unavailable")
    assert agents.chat.composer.draft() == "Retry this in Chat"
    assert "CLI unavailable" in agents.chat.composer.note.text()
    assert not agents.chat.turns


def test_reopening_keeps_unsent_folder_and_connection_choices(make, tmp_path):
    core = FakeCore(connections=[_connection(), _connection("second", "Second")])
    popup, _, _ = make(core)
    assert not popup.composer.settings_button.isChecked()
    popup.composer.set_options_open(True)
    popup.composer.set_draft("Keep all my choices")
    popup.composer.connection.setCurrentIndex(1)
    popup.composer.connection.activated.emit(1)
    folder = tmp_path / "another-folder"
    folder.mkdir()
    popup.composer.folder.setEditText(str(folder))
    popup.dismiss()
    popup.open_over(popup.parentWidget())
    assert not popup.composer.settings_button.isChecked()
    assert not popup.composer.options.isVisible()
    popup.agents._set_connections(core.connections)
    assert not popup.composer.settings_button.isChecked()
    assert popup.composer.settings()["folder"] == str(folder)
    assert popup.composer.connection.currentData() == "second"


def test_late_model_response_does_not_replace_new_provider_models(make):
    core = FakeCore(connections=[_connection(), _connection("other", "Other", kind="other")])
    popup, agents, _ = make(core)
    callbacks = []
    agents.ask = lambda kind, work, done: callbacks.append(done)
    model = popup.composer.model
    model._load()
    popup.composer.connection.setCurrentIndex(1)
    popup.composer.connection.activated.emit(1)
    model._load()
    callbacks[1]({"ok": True, "models": [{"value": "new", "label": "New"}]})
    callbacks[0]({"ok": True, "models": [{"value": "old", "label": "Old"}]})
    assert model.findData("new") >= 0
    assert model.findData("old") == -1


def test_combo_popup_does_not_dismiss_composer(make, qapp):
    popup, _, _ = make()
    popup.composer.set_options_open(True)
    popup.composer.connection.showPopup()
    qapp.processEvents()
    assert popup.isVisible()
    popup.composer.connection.hidePopup()


@pytest.mark.parametrize("payload", [
    {"ok": True, "sessions": [_session(str(i)) for i in range(5)]},
    {"ok": True, "sessions": []},
    {"ok": False, "error": "History unavailable"},
])
def test_delayed_history_never_resizes_window_and_shows_skeleton(make, qapp, payload):
    popup, agents, _ = make()
    popup.dismiss()
    qapp.processEvents()
    original = agents.ask
    replies = []

    def ask(kind, work, done):
        if kind == "quick-agent-sessions":
            replies.append(done)
        else:
            original(kind, work, done)

    agents.ask = ask
    popup.open_over(popup.parentWidget())
    size = popup.size()
    assert popup.minimumSize() == size == popup.maximumSize()
    QTest.qWait(popup.recent_skeleton.DELAY_MS + 40)
    assert popup.recent_skeleton.isVisible()
    assert not popup.recent.isEnabled()
    QTest.keyClicks(popup.composer.text, "Typing while history loads")
    popup.composer.set_options_open(True)
    popup.composer.set_draft("Long draft\n" * 20)
    QTest.qWait(30)  # Settle the editor's layout before delivering the history read.
    region = popup.recent.geometry()
    assert popup.size() == size
    replies.pop()(payload)
    qapp.processEvents()
    assert not popup.recent_skeleton.active()
    assert popup.recent.isEnabled()
    assert popup.recent.geometry() == region
    popup._tick.timeout.emit()
    popup.composer.set_options_open(False)
    qapp.processEvents()
    assert popup.size() == size
    popup.dismiss()
    qapp.processEvents()
    popup.open_over(popup.parentWidget())
    qapp.processEvents()
    assert popup.size() == size
    assert not popup.composer.settings_button.isChecked()
    popup.dismiss()
    assert not popup.recent_skeleton.active()
    replies.pop()(payload)
    assert not popup.isVisible()
