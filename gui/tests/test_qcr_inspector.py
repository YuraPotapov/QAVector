"""The QCR Inspector (ADR-002 2.5): the Log page's Executions mode.

A stand-in host answers the way the real one does; what is checked is what the
person sees - every no-data state in its own words, the list kept live by the
host's pushes, one action's events, Cancel only while it can do something, and
the table recipe's symmetry (a search box over every column, on its edges).
"""

import pytest
from PySide6.QtCore import QObject, Signal

from cms_gui import qcrhost
from cms_gui.pages import executions as ex
from cms_gui.pages.log import EXECUTIONS, LAUNCHER, LogPage

WEB = "a" * 32


def execution(execution_id="e1", state="succeeded", op="start", actor="gui-user", **extra):
    one = {"execution_id": execution_id, "capability_id": "services.%s.%s" % (WEB, op), "resource_id": WEB,
           "actor_id": actor, "state": state, "created_at": 1000.0,
           "finished_at": 1000.03 if state in qcrhost.TERMINAL else None,
           "output": {"service_status": "running", "readiness": "not_checked"} if state == "succeeded" else None,
           "error": None}
    one.update(extra)
    return one


class Host(QObject):
    execution_changed = Signal(dict)
    state_changed = Signal(str)

    def __init__(self, state=qcrhost.READY, executions=(), events=None):
        super().__init__()
        self.state, self.problem = state, ""
        self.executions, self.events = list(executions), events or {}
        self.asked = []

    def ask(self, kind, done, **fields):
        self.asked.append((kind, fields))
        if kind == "list":
            done({"ok": True, "executions": list(self.executions)})
        elif kind == "events":
            done({"ok": True, "events": self.events.get(fields["execution_id"], [])})
        elif kind == "cancel":
            done({"ok": True})

    def become(self, state, problem=""):
        self.state, self.problem = state, problem
        self.state_changed.emit(state)


@pytest.fixture
def view(qapp):
    one = ex.ExecutionsView()
    one.resize(1100, 700)
    one.show()
    yield one
    one.close()


def shown(view):
    return [view.table.item(row, column).text() for row in range(view.table.rowCount())
            if not view.table.isRowHidden(row) for column in range(view.table.columnCount())]


def note(view):
    return view.note.text() if view.note.isVisible() else ""


@pytest.mark.parametrize("state, problem, words", [
    (qcrhost.OFF, "", ex.NOTE_OFF),
    (qcrhost.STARTING, "", ex.NOTE_STARTING),
    (qcrhost.DOWN, "The QCR host keeps stopping.", "The QCR host keeps stopping."),
    (qcrhost.READY, "", ex.NOTE_EMPTY),
])
def test_every_empty_state_says_what_it_is(view, state, problem, words):
    host = Host(state)
    host.problem = problem
    view.set_host(host)
    assert view.table.rowCount() == 0 and note(view) == words
    assert view.count.text() == ""


def test_the_list_says_who_did_what_and_how_it_ended(view):
    host = Host(executions=[execution("e2", "failed", op="stop", actor="cycle:r1",
                                      error={"code": "busy", "message": "x"}),
                            execution("e1")])
    view.set_host(host, name_of=lambda resource_id: "Demo Shop · Web")
    first, second = [view.table.item(0, c).text() for c in range(7)], [view.table.item(1, c).text() for c in range(7)]
    assert first[1:] == ["Demo Shop · Web", "stop", "cycle r1", "failed", "0.03 s",
                         "another action on this service is still running"]
    assert second[1:] == ["Demo Shop · Web", "start", "you", "succeeded", "0.03 s", "service running"]
    assert view.count.text() == "2" and note(view) == ""


def test_a_push_updates_in_place_or_comes_in_on_top(view):
    host = Host(executions=[execution("e1", "dispatched")])
    view.set_host(host)
    assert view.table.item(0, ex.C_TOOK).text() == "under way"
    host.execution_changed.emit(execution("e1", "succeeded"))
    assert view.table.rowCount() == 1 and view.table.item(0, ex.C_STATE).text() == "succeeded"
    host.execution_changed.emit(execution("e2", "queued", op="restart"))
    assert [view.table.item(r, ex.C_ACTION).text() for r in range(2)] == ["restart", "start"]
    assert ("list", {"filters": {"limit": ex.LIMIT}}) in host.asked and \
        sum(1 for kind, _ in host.asked if kind == "list") == 1, "pushes do not ask again"


def test_selecting_an_action_shows_its_steps_and_nothing_selected_says_so(view):
    events = {"e1": [{"cursor": 1, "type": "state", "state": "queued", "at": 1000.0},
                     {"cursor": 2, "type": "state", "state": "dispatched", "from": "queued", "at": 1000.01,
                      "payload": {"service_status": "starting"}},
                     {"cursor": 3, "type": "owner.status", "at": 1000.02, "payload": {"status": "running"}}]}
    host = Host(executions=[execution("e1")], events=events)
    view.set_host(host)
    assert view.events.rowCount() == 0 and view.events_note.text() == ex.NOTE_NO_SELECTION
    view.table.selectRow(0)
    rows = [[view.events.item(r, c).text() for c in range(4)] for r in range(view.events.rowCount())]
    assert [row[1:3] for row in rows] == [["state", "queued"], ["state", "taken by the page"], ["page: status", "-"]]
    assert rows[1][3] == 'from queued; {"service_status": "starting"}'
    view.table.clearSelection()
    assert view.events.rowCount() == 0


def test_cancel_only_while_the_action_is_under_way(view):
    host = Host(executions=[execution("e1", "running"), execution("e0")])
    view.set_host(host)
    assert not view.cancel_button.isEnabled()
    view.table.selectRow(1)
    assert not view.cancel_button.isEnabled(), "a finished action has nothing to cancel"
    view.table.selectRow(0)
    assert view.cancel_button.isEnabled()
    view.cancel_button.click()
    assert ("cancel", {"execution_id": "e1", "reason": "Canceled from the Executions view."}) in host.asked


def test_a_search_that_matches_nothing_says_so_and_a_service_can_be_shown_alone(view):
    host = Host(executions=[execution("e1"), execution("e2", resource_id="b" * 32)])
    view.set_host(host, name_of=lambda resource_id: "Shop · Web" if resource_id == WEB else "Shop · Db")
    view.show_service("Shop · Web")
    assert view.count.text() == "1 of 2"
    view.search.box(ex.C_ACTOR).setText("nobody")
    assert note(view) == ex.NOTE_FILTERED


def test_a_search_box_sits_over_every_column_on_its_edges(view, qapp):
    view.set_host(Host(executions=[execution("e1")]))
    qapp.processEvents()
    for table, search in ((view.table, view.search), (view.events, view.events_search)):
        header = table.horizontalHeader()
        assert all(search.box(column) is not None for column in range(table.columnCount()))
        for column in range(table.columnCount()):
            assert search.box(column).geometry().left() == header.sectionViewportPosition(column)


def test_the_host_coming_up_fills_the_list(view):
    host = Host(qcrhost.STARTING, executions=[execution("e1")])
    view.set_host(host)
    assert view.table.rowCount() == 0 and note(view) == ex.NOTE_STARTING
    host.become(qcrhost.READY)
    assert view.table.rowCount() == 1
    host.become(qcrhost.DOWN, "gone")
    assert view.table.rowCount() == 0 and note(view) == "gone"


def test_the_log_page_switches_modes_and_remembers(qapp, tmp_path, monkeypatch):
    class Settings:
        log_mode = "executions"
        log_source = ""
    settings = Settings()
    page = LogPage(settings)
    try:
        assert page.mode.current() == EXECUTIONS and page.stack.currentWidget() is page.executions
        assert page.launcher_tools.isHidden() and page.showing.isHidden()
        page.mode.set_current(LAUNCHER)
        assert page.stack.currentWidget() is page.view and settings.log_mode == "launcher"
        assert not page.launcher_tools.isHidden()
        page.show_executions("Shop · Web")
        assert settings.log_mode == "executions"
        assert page.executions.search.box(ex.C_SERVICE).text() == "Shop · Web"
    finally:
        page.close()


def _three(view):
    rows = [execution("e3", "succeeded", op="stop", created_at=1003.0, finished_at=1003.5),
            execution("e2", "succeeded", op="restart", created_at=1002.0, finished_at=1002.1),
            execution("e1", "succeeded", op="start", created_at=1001.0, finished_at=1001.1),
            execution("x9", "succeeded", op="start", resource_id="b" * 32, created_at=1004.0, finished_at=1004.1)]
    host = Host(executions=rows)
    view.set_host(host, name_of=lambda resource_id: "Shop · Web" if resource_id == WEB else "Shop · Db")
    return host


def selected_id(view):
    rows = view.table.selectionModel().selectedRows()
    return view.table.item(rows[0].row(), ex.C_TIME).data(ex.ID_ROLE) if rows else None


def test_a_running_service_opens_on_the_start_of_its_current_run(view):
    _three(view)
    view.table.selectRow(3)                         # what was clicked before does not stay
    view.show_service("Shop · Web", running=True)
    assert selected_id(view) == "e2"                # the restart, not the newer stop row
    assert view._selected == "e2", "its steps are the ones shown below"


def test_a_stopped_service_opens_on_its_newest_action(view):
    _three(view)
    view.table.selectRow(2)
    view.show_service("Shop · Web", running=False)
    assert selected_id(view) == "e3"


def test_the_row_is_chosen_when_the_list_arrives_later(view):
    host = Host(qcrhost.STARTING, executions=[execution("e2", op="restart", created_at=1002.0),
                                               execution("e1", created_at=1001.0)])
    view.set_host(host)
    view.show_service("", running=True)
    assert selected_id(view) is None
    host.become(qcrhost.READY)
    assert selected_id(view) == "e2"
