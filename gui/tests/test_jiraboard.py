from PySide6.QtCore import QMimeData, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QInputDialog, QLineEdit, QListWidget, QStyleOptionViewItem

from cms_gui.pages.jira import JiraView
from cms_gui.pages import jiraboard
from cms_gui.pages.jiraboard import DETAILS_ROLE, MIME, SELECTED_ROLE, CardDelegate, Column, assignee_colors, column_order
from cms_gui.pages.jiradialogs import TransitionFieldsDialog


class Settings:
    def __init__(self):
        self.state = {}

    def integrations_state(self):
        return self.state

    def save_integrations_state(self, state):
        self.state = state

    def splitter(self, _):
        return []

    def fold(self, _):
        return None


class BoardCore:
    connection = {"id": "qa", "name": "QA", "token_set": True, "flavour": "cloud",
                  "settings": {"refresh_minutes": 1}}

    def __init__(self):
        self.calls = []
        self.failure = None
        self.moved = False

    def integrations(self, verb, connection=None, operation=None, *, document=None, cancel=None):
        self.calls.append((operation or verb, document))
        if verb == "list":
            return {"ok": True, "connections": [self.connection]}
        if verb == "save":
            self.connection = document["connection"]
            return {"ok": True, "connection": self.connection}
        row = {"key": "QA-1", "summary": "Example", "updated": "seen"}
        if operation == "search":
            return {"ok": True, "result": {"rows": [row]}}
        if operation in ("issue", "board_issue"):
            return {"ok": True, "result": {"issue": row}}
        if operation == "board_people":
            return {"ok": True, "result": {"choices": [{"value": "second-user", "label": "Tester"}]}}
        if operation == "board_catalog":
            return {"ok": True, "result": {"boards": [{"id": "47", "name": "QA workflow"}]}}
        if operation == "board_move":
            if self.failure:
                return {"ok": False, "kind": "forbidden", "error": self.failure}
            self.moved = True
            return {"ok": True, "result": {"issue": dict(row, status="Done")}}
        if operation in ("board_search", "board_reload"):
            field = document["field"]
            fields = [{"id": "status", "name": "Status", "kind": "status", "editable": True},
                      {"id": "customfield_18", "name": "Estimate", "kind": "number", "editable": True},
                      {"id": "assignee", "name": "Assignee", "kind": "user", "editable": True}]
            choices = [{"value": "1", "label": "To Do"}, {"value": "2", "label": "Done"}]
            group = choices[0] if field == "status" else {"value": 5, "label": "5"}
            rows = [] if self.moved else [dict(row, group=group, issue_id="120")]
            if field == "assignee":
                rows = [dict(row, key=key, group={"value": None, "label": "No value"}, issue_id="120")
                        for key in ("QA-1", "WEB-1")]
            return {"ok": True, "result": {"rows": rows, "choices": choices if field == "status" else [],
                                           "fields": fields, "field": field, "cursor": None,
                                           "contexts": [], "jql": "status = Open"}}
        raise AssertionError(operation)


class Drop:
    def __init__(self, source, key):
        self.column = source
        self.mime = QMimeData()
        self.mime.setData(MIME, key.encode())
        self.accepted = False

    def source(self):
        return self.column

    def mimeData(self):
        return self.mime

    def acceptProposedAction(self):
        self.accepted = True


def _column(view, label):
    return next(column for column in view.board.scroller.widget().findChildren(Column)
                if column.accessibleName() == label)


def _wait_for_move(board):
    for _ in range(100):
        if not board.writing:
            return
        QTest.qWait(15)
    assert not board.writing


def test_board_only_reads_on_user_action_even_if_issues_auto_refresh_is_enabled(qapp):
    view = JiraView(Settings())
    core = BoardCore()
    view.set_core(core)
    view.show()
    try:
        assert view.auto.isActive()
        view.auto.setInterval(5)
        view.tabs.setCurrentWidget(view.board)
        assert [kind for kind, _ in core.calls] == ["list", "search", "board_search"]
        assert not view.auto.isActive()
        view._auto_refresh()  # A delivery already queued as the user switched tabs.
        QTest.qWait(25)
        assert len(core.calls) == 3
        view.tabs.setCurrentWidget(view.issues)
        assert view.auto.isActive()
        view.tabs.setCurrentWidget(view.board)
        assert len(core.calls) == 3
        view.board.refresh.click()
        assert [kind for kind, _ in core.calls][-1] == "board_search"
        assert len(core.calls) == 4
    finally:
        view.close()


def test_board_shows_skeletons_during_search_and_pagination(qapp, monkeypatch):
    view = JiraView(Settings())
    core = BoardCore()
    pending = []
    call = view._call

    def defer_board(kind, work, done, **kwargs):
        if kind == "board:board_search":
            pending.append((work, done))
        else:
            call(kind, work, done, **kwargs)

    monkeypatch.setattr(view, "_call", defer_board)
    board = view.board
    monkeypatch.setattr(board.skeleton, "DELAY_MS", 0)
    monkeypatch.setattr(board.more_skeleton, "DELAY_MS", 0)
    view.set_core(core)
    view.resize(980, 530)
    view.show()
    try:
        view.tabs.setCurrentWidget(board)
        QTest.qWait(1)
        assert board.skeleton.isVisible() and not board.more_skeleton.active()
        assert len(pending) == 1

        view.tabs.setCurrentWidget(view.issues)
        assert not board.skeleton.active()
        view.tabs.setCurrentWidget(board)
        QTest.qWait(1)
        assert board.skeleton.isVisible() and len(pending) == 1

        work, done = pending.pop()
        first = work(core, None)
        first["result"]["cursor"] = "second-page"
        done(first)
        qapp.processEvents()
        assert not board.skeleton.active()
        assert board.message.isHidden()
        assert board.scroller.geometry().bottom() == board.rect().bottom()
        assert board.more.isVisible() and board.more.isEnabled()
        toolbar = board.layout().itemAt(0).layout()
        assert toolbar.indexOf(board.more) == toolbar.indexOf(board.refresh) + 1

        todo = _column(view, "To Do")
        board.more.click()
        QTest.qWait(1)
        assert board.more_skeleton.isVisible() and not board.skeleton.active()
        assert board.message.isVisible()
        assert not board.more.isEnabled()
        assert _column(view, "To Do") is todo and todo.count() == 1
        work, done = pending.pop()
        second = work(core, None)
        assert core.calls[-1][1]["cursor"] == "second-page"
        second["result"]["rows"][0]["key"] = "QA-2"
        done(second)
        assert not board.more_skeleton.active() and not board.more.isVisible()
        assert board.message.isHidden()
        assert _column(view, "To Do").count() == 2

        board.refresh.click()
        QTest.qWait(1)
        assert board.skeleton.isVisible()
        _, done = pending.pop()
        done({"ok": False, "kind": "invalid", "error": "Could not refresh."})
        assert not board.skeleton.active() and board.refresh.isEnabled()
        assert board.message.isVisible()
        assert board.message.text() == "Could not refresh."
        assert _column(view, "To Do").count() == 2
    finally:
        view.close()


def test_cards_keep_their_size_with_long_titles_and_optional_details(qapp, monkeypatch):
    view = JiraView(Settings())
    core = BoardCore()
    answer = core.integrations
    summary = "Jira integration and a long issue title " * 8

    def with_long_cards(*args, **kwargs):
        payload = answer(*args, **kwargs)
        if len(args) == 3 and args[2] == "board_search":
            row = payload["result"]["rows"][0]
            done = {"value": "2", "label": "Done"}
            payload["result"]["rows"] += [
                dict(row, key=f"QA-{index}", group=done, summary=summary, assignee="Olena",
                     assignee_id="acct-olena", priority="High", blocked=True,
                     story_points=0, due_date="2026-11-25")
                for index in range(2, 15)]
        return payload

    monkeypatch.setattr(core, "integrations", with_long_cards)
    view.set_core(core)
    view.resize(950, 530)
    view.show()
    try:
        view.tabs.setCurrentWidget(view.board)
        qapp.processEvents()
        todo, done = _column(view, "To Do"), _column(view, "Done")
        assert done.verticalScrollBar().maximum() > 0
        assert not done.horizontalScrollBar().isVisible()
        short_rect = CardDelegate.bounds(todo.visualItemRect(todo.item(0)))
        long_rect = CardDelegate.bounds(done.visualItemRect(done.item(0)))
        assert short_rect.size() == long_rect.size()
        assert long_rect.right() < done.viewport().width()
        assert done.visualItemRect(done.item(1)).top() - long_rect.bottom() == CardDelegate.GAP + 1
        lines = CardDelegate.summary_lines(summary, done.fontMetrics(), 196)
        assert len(lines) == 2 and lines[-1].endswith("…")
        assert done.item(0).data(DETAILS_ROLE)["story_points"] == 0
        for detail in ("Olena", "High", "Blocker", "Story Points 0", "2026-11-25"):
            assert detail in done.item(0).toolTip()
    finally:
        view.close()


def test_board_orders_status_columns_by_workflow_or_selected_jira_board(qapp, monkeypatch):
    view = JiraView(Settings())
    core = BoardCore()
    answer = core.integrations

    def unordered_statuses(*args, **kwargs):
        payload = answer(*args, **kwargs)
        if len(args) == 3 and args[2] == "board_search":
            payload["result"]["choices"] = [
                {"value": "2", "label": "Done", "category": "done"},
                {"value": "3", "label": "Review", "category": "indeterminate"},
                {"value": "8", "label": "ON PROD", "category": "indeterminate"},
                {"value": "1", "label": "To Do", "category": "new"},
                {"value": "4", "label": "In Progress", "category": "indeterminate"},
                {"value": "5", "label": "Draft", "category": "indeterminate"},
                {"value": "6", "label": "Reopen", "category": "indeterminate"},
                {"value": "7", "label": "READY FOR PROD (RELEASE)", "category": "indeterminate"},
            ]
            if kwargs["document"].get("board_id") == "47":
                payload["result"].update(status_order=["5", "1", "6", "4", "7", "8", "3", "2"],
                                         source_board="QA workflow")
        return payload

    monkeypatch.setattr(core, "integrations", unordered_statuses)
    view.set_core(core)
    view.show()
    try:
        view.tabs.setCurrentWidget(view.board)
        columns = view.board.scroller.widget().findChildren(Column)
        assert [column.accessibleName() for column in columns] == [
            "Draft", "To Do", "Reopen", "In Progress", "Review", "READY FOR PROD (RELEASE)",
            "ON PROD", "Done", "No value"]
        view.board.board_order.wanted.emit()
        index = view.board.board_order.findData("47")
        assert index >= 0
        view.board.board_order.setCurrentIndex(index)
        view.board.board_order.activated.emit(index)
        columns = view.board.scroller.widget().findChildren(Column)
        assert [column.accessibleName() for column in columns] == [
            "Draft", "To Do", "Reopen", "In Progress", "READY FOR PROD (RELEASE)",
            "ON PROD", "Review", "Done", "No value"]
        assert view.settings.integrations_state()["board"]["qa"]["board_id"] == "47"
        assert view.board.board_order.toolTip() == "Using status order from QA workflow."
    finally:
        view.close()


def test_board_sorts_number_columns_by_value_and_keeps_account_colors_stable():
    columns = [("10", {"value": 10}), ("2", {"value": 2}), ("null", {"value": None})]
    assert sorted(columns, key=lambda entry: column_order("number", *entry)) == [columns[1], columns[0], columns[2]]
    assert assignee_colors("acct-olena") == assignee_colors("acct-olena")
    assert assignee_colors("acct-olena") != assignee_colors("acct-yurii")


def test_board_drop_uses_target_id_refreshes_membership_and_leaves_card_on_refusal(qapp):
    view = JiraView(Settings())
    core = BoardCore()
    view.set_core(core)
    view.show()
    try:
        view.tabs.setCurrentWidget(view.board)
        assert _column(view, "Done").count() == 0
        core.failure = "Workflow denied the transition."
        drop = Drop(_column(view, "To Do"), "QA-1")
        _column(view, "Done").dropEvent(drop)
        assert drop.accepted
        assert _column(view, "To Do").count() == 1
        assert view.board.message.text() == core.failure
        assert core.calls[-1] == ("board_move", {"field": "status", "key": "QA-1",
                                                  "from": "1", "value": "2", "updated": "seen"})
        core.failure = None
        drop = Drop(_column(view, "To Do"), "QA-1")
        _column(view, "Done").dropEvent(drop)
        assert core.calls[-1][0] == "board_reload"
        assert _column(view, "To Do").count() == 0
        assert view._cache(view.current()).details["QA-1"]["status"] == "Done"
    finally:
        view.close()


def test_ctrl_click_moves_multiple_columns_with_separate_retry_limits(qapp, monkeypatch):
    view = JiraView(Settings())
    core = BoardCore()
    answer = core.integrations
    requests = {"QA-1": [], "QA-2": []}
    applied = set()

    def integrations(*args, **kwargs):
        operation = args[2] if len(args) == 3 else None
        document = kwargs.get("document") or {}
        if operation == "board_move":
            key = document["key"]
            requests[key].append(document)
            if key == "QA-2":
                return {"ok": False, "kind": "unknown_outcome", "error": "Jira lost the reply."}
            if len(requests[key]) < 3:
                return {"ok": False, "kind": "unreachable", "error": "Connection lost before write."}
            applied.add(key)
            return {"ok": True, "result": {}}

        payload = answer(*args, **kwargs)
        if operation in {"board_search", "board_reload"}:
            result = payload["result"]
            row = result["rows"][0]
            review = {"value": "3", "label": "Review"}
            result["choices"].append(review)
            result["rows"].append(dict(row, key="QA-2", group=review, issue_id="121"))
            if applied:
                row["group"] = {"value": "2", "label": "Done"}
        return payload

    monkeypatch.setattr(core, "integrations", integrations)
    monkeypatch.setattr(jiraboard, "MOVE_RETRY_DELAY_MS", 1)
    view.set_core(core)
    view.show()
    try:
        view.tabs.setCurrentWidget(view.board)
        todo, review = _column(view, "To Do"), _column(view, "Review")
        QTest.mouseClick(todo.viewport(), Qt.LeftButton, Qt.NoModifier,
                         todo.visualItemRect(todo.item(0)).center())
        QTest.mouseClick(review.viewport(), Qt.LeftButton, Qt.ControlModifier,
                         review.visualItemRect(review.item(0)).center())
        assert view.board.state.selected == {"QA-1", "QA-2"}
        assert todo.item(0).data(SELECTED_ROLE) and review.item(0).data(SELECTED_ROLE)
        assert todo.currentItem() is todo.item(0)
        _column(view, "Done").dropEvent(Drop(todo, "QA-1"))
        assert view.board.writing and view.busy_reason()
        assert _column(view, "To Do").count() == 1
        assert _column(view, "Review").count() == 1
        _wait_for_move(view.board)
        assert [bool(request.get("confirm_only")) for request in requests["QA-1"]] == [False, False, False]
        assert [bool(request.get("confirm_only")) for request in requests["QA-2"]] == [False, True, True]
        assert core.calls[-1][1]["issue_ids"] == ["120", "121"]
        assert _column(view, "Done").count() == 1 and _column(view, "Review").count() == 1
        assert view.board.state.selected == {"QA-2"}
        assert "Moved 1 of 2 issues" in view.board.message.text()
        assert "QA-2" in view.board.message.toolTip()
    finally:
        view.close()


def test_unknown_move_and_failed_reload_keep_card_in_its_original_column(qapp, monkeypatch):
    view = JiraView(Settings())
    core = BoardCore()
    answer = core.integrations
    moves = []

    def integrations(*args, **kwargs):
        operation = args[2] if len(args) == 3 else None
        if operation == "board_move":
            moves.append(kwargs["document"])
            return {"ok": False, "kind": "unknown_outcome", "error": "No Jira reply."}
        if operation == "board_reload":
            return {"ok": False, "kind": "unreachable", "error": "No connection."}
        return answer(*args, **kwargs)

    monkeypatch.setattr(core, "integrations", integrations)
    monkeypatch.setattr(jiraboard, "MOVE_RETRY_DELAY_MS", 1)
    view.set_core(core)
    view.show()
    try:
        view.tabs.setCurrentWidget(view.board)
        _column(view, "Done").dropEvent(Drop(_column(view, "To Do"), "QA-1"))
        _wait_for_move(view.board)
        assert _column(view, "To Do").count() == 1 and _column(view, "Done").count() == 0
        assert len(moves) == 3 and all(move["confirm_only"] for move in moves[1:])
        _column(view, "Done").dropEvent(Drop(_column(view, "To Do"), "QA-1"))
        assert len(moves) == 3
        assert "Refresh the Board" in view.board.message.text()
    finally:
        view.close()


def test_board_persists_free_entry_columns_for_the_selected_field(qapp, monkeypatch):
    settings = Settings()
    view = JiraView(settings)
    core = BoardCore()
    view.set_core(core)
    view.show()
    try:
        view.tabs.setCurrentWidget(view.board)
        index = view.board.group.findData("customfield_18")
        view.board.group.setCurrentIndex(index)
        view.board.group.activated.emit(index)
        monkeypatch.setattr(QInputDialog, "getText", lambda *args: ("13.5", True))
        view.board.add_column.click()
        assert _column(view, "13.5").count() == 0
        assert settings.state["board"]["qa"]["columns"]["customfield_18"] == [
            {"value": 13.5, "label": "13.5"}]
        _column(view, "13.5").dropEvent(Drop(_column(view, "5"), "QA-1"))
        assert core.calls[-2][1]["value"] == 13.5
    finally:
        view.close()


def test_scope_change_invalidates_cards_without_background_fetch(qapp):
    view = JiraView(Settings())
    core = BoardCore()
    view.set_core(core)
    view.show()
    try:
        view.tabs.setCurrentWidget(view.board)
        assert view.board.state.rows[0]["key"] == "QA-1"
        saved = dict(core.connection, settings={"predefined_query": "project = WEB"})
        view.save_connection({"connection": saved})
        assert not view.board.state.rows
        assert core.calls[-1][0] == "save"
        view.board.refresh.click()
        assert core.calls[-1][0] == "board_search"
        _column(view, "To Do").itemActivated.emit(_column(view, "To Do").item(0))
        assert core.calls[-1][0] == "board_issue"
    finally:
        view.close()


def test_add_assignee_column_can_search_using_another_projects_card(qapp, monkeypatch):
    view = JiraView(Settings())
    core = BoardCore()
    view.set_core(core)
    view.show()
    try:
        view.tabs.setCurrentWidget(view.board)
        index = view.board.group.findData("assignee")
        view.board.group.setCurrentIndex(index)
        view.board.group.activated.emit(index)
        monkeypatch.setattr(QInputDialog, "getText", lambda *args: ("Tester", True))
        monkeypatch.setattr(QInputDialog, "getItem", lambda *args, **kw: (args[3][-1], True))
        view.board.add_column.click()
        assert core.calls[-1] == ("board_people", {"field": "assignee", "key": "WEB-1", "query": "Tester"})
        assert _column(view, "Tester").count() == 0
    finally:
        view.close()


def test_transition_fields_dialog_accepts_required_text_and_multiple_options(qapp):
    dialog = TransitionFieldsDialog(None, "Finish", [
        {"key": "customfield_20", "name": "Reason", "allowed": [], "kind": "text"},
        {"key": "components", "name": "Components", "multiple": True,
         "allowed": [{"id": "15", "name": "API"}, {"id": "16", "name": "UI"}]}])
    dialog.move.click()
    assert dialog.result() == 0
    dialog.findChild(QLineEdit).setText("Verified")
    choices = dialog.findChild(QListWidget)
    for index in range(choices.count()):
        choices.item(index).setCheckState(Qt.Checked)
    dialog.move.click()
    assert dialog.result() == 1
    assert dialog.fields() == {"customfield_20": "Verified", "components": ["15", "16"]}
