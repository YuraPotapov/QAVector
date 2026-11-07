"""Integrations -> Jira: what the page shows, and - above all - what it asks.

The rule these tests hold the page to is the user's own: nothing is asked of
Jira unless a person did something that asks, and then only what is on screen.
So the fake core counts every call, and most tests are about the count.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QDialog, QPushButton

from cms_gui import theme, widgets
from cms_gui.pages import jira as jira_module
from cms_gui.pages.integrations import IntegrationsPage
from cms_gui.pages.jiraconnections import ConnectionDialog
from cms_gui.settings import Settings

TOKEN = "tok-gui-not-a-real-one"


def _connection(connection_id="product-team", name="Product team", token=True, **extra):
    return dict({"id": connection_id, "provider": "jira", "name": name,
                 "site": "https://qa.example.invalid", "flavour": "cloud", "auth": "basic",
                 "email": "you@yourcompany.example", "verify_tls": True,
                 "settings": {"default_project": "", "refresh_minutes": 0},
                 "token_set": token}, **extra)


def _row(key, summary="Session expires", status="In Progress", category="indeterminate"):
    return {"key": key, "summary": summary, "type": "Bug", "priority": "High",
            "status": status, "category": category, "assignee": "Alex",
            "updated": "2026-11-05T10:00:00.000+0000",
            "url": "https://qa.example.invalid/browse/" + key}


def _issue(key, **extra):
    issue = dict(_row(key), reporter="Olena", labels=["regression"],
                 project={"key": "QA", "name": "Quality"},
                 description_html="<p>Steps</p>", attachments=[], transitions=[],
                 comments={"items": [], "total": 0, "start": 0})
    issue.update(extra)
    return issue


class FakeCore:
    """Answers ``--integrations`` from canned payloads and writes every call down."""

    offload = False

    def __init__(self, connections=None, rows=None, **answers):
        self.connections = list(connections if connections is not None else [_connection()])
        self.rows = list(rows if rows is not None else [_row("QA-142"), _row("QA-139")])
        self.answers = answers
        self.calls = []

    def integrations(self, *args, document=None, timeout=120, cancel=None):
        self.calls.append((args, document))
        command = args[0]
        if command == "list":
            return {"ok": True, "connections": [dict(one) for one in self.connections]}
        if command == "save":
            saved = dict(document["connection"])
            saved.setdefault("id", saved["name"].lower().replace(" ", "-"))
            saved["token_set"] = bool(document.get("token")) or any(
                one.get("token_set") for one in self.connections if one["id"] == saved["id"])
            return self.answers.get("save") or {"ok": True, "connection": saved}
        if command == "delete":
            return {"ok": True, "id": args[1]}
        if command == "test":
            return self.answers.get("test") or {"ok": True, "result": {"account": "Alex"}}
        if command == "sign-in":
            self.cancel = cancel
            return self.answers.get("sign-in") or {
                "ok": True, "account": "Alex", "pending": "pending:abc",
                "sites": [{"id": "cloud-1", "name": "yourcompany",
                           "url": "https://yourcompany.atlassian.net"}]}
        if command == "app":
            return {"ok": True, "app": {"client_id": "client-1", "port": 47321,
                                        "secret_set": True,
                                        "callback": "http://localhost:47321/callback"}}
        if command == "app-save":
            return {"ok": True, "app": dict(document, secret_set=True)}
        if command == "activity":
            return {"ok": True, "activity": list(self.answers.get("activity_rows") or [])}
        operation = args[2]
        answer = self.answers.get(operation)
        if callable(answer):
            return answer(document)
        if answer is not None:
            return answer
        if operation == "search":
            return {"ok": True, "result": {"rows": self.rows, "cursor": None}}
        if operation == "issue":
            return {"ok": True, "result": {"issue": _issue(document["key"])}}
        if operation == "refresh":
            issue = document.get("issue")
            return {"ok": True, "result": {
                "list": {"rows": self.rows, "cursor": None},
                "issue": _issue(issue["key"]) if issue else None}}
        if operation == "projects":
            return {"ok": True, "result": {"projects": [{"key": "QA", "name": "Quality"}]}}
        if operation == "statuses":
            return {"ok": True, "result": {"statuses": [{"name": "To Do", "category": "new"}]}}
        if operation == "comments":
            return {"ok": True, "result": {"comments": {"items": [], "total": 30,
                                                        "start": document["start"]}}}
        if operation in ("transition", "comment", "edit", "assign", "attach"):
            changed = {"transition": {"status": "Done", "category": "done"},
                       "assign": {"assignee": "Alex"},
                       "edit": dict(document.get("fields") or {})}.get(operation, {})
            changed.pop("description", None)
            return {"ok": True, "result": {"issue": _issue(document["key"], **changed),
                                           "summary": "%s: %s" % (document["key"], operation)}}
        if operation == "mentionable":
            people = [{"id": "acc-olena", "name": "Olena"}, {"id": "acc-maksym", "name": "Maksym"}]
            query = (document.get("query") or "").lower()
            if query == "taras":
                people = [{"id": "acc-taras", "name": "Taras"}]
            return {"ok": True, "result": {"people": [
                one for one in people if query in one["name"].lower()]}}
        if operation == "assignable":
            people = [{"id": "acc-olena", "name": "Olena"}, {"id": "acc-maksym", "name": "Maksym"}]
            query = (document.get("query") or "").lower()
            return {"ok": True, "result": {"people": [
                one for one in people if query in one["name"].lower()]}}
        if operation == "edit_meta":
            return {"ok": True, "result": {"editable": ["summary", "labels", "priority"],
                                           "priorities": [{"id": "2", "name": "High"},
                                                          {"id": "3", "name": "Medium"}]}}
        if operation == "create_meta":
            if document.get("type"):
                return {"ok": True, "result": {"fields": [
                    {"key": "summary", "name": "Summary", "required": True, "type": "string",
                     "allowed": []},
                    {"key": "priority", "name": "Priority", "required": False,
                     "type": "priority", "allowed": [{"id": "3", "name": "Medium"}]}]}}
            return {"ok": True, "result": {"types": [{"id": "10001", "name": "Bug",
                                                      "subtask": False}]}}
        if operation == "create":
            return {"ok": True, "result": {"issue": _issue("QA-200", summary=document[
                "fields"]["summary"]), "summary": "QA-200: created"}}
        if operation == "download":
            return {"ok": True, "result": {"path": document["to"], "bytes": 3}}
        raise AssertionError("unexpected operation %s" % operation)

    def asked(self):
        """What reached Jira: every operation and Test, in order."""
        return [(args[2] if args[0] == "call" else args[0]) for args, _doc in self.calls
                if args[0] in ("call", "test")]


@pytest.fixture
def settings(tmp_path):
    made = Settings()
    made._qs = QSettings(str(tmp_path / "gui.ini"), QSettings.IniFormat)
    return made


@pytest.fixture
def make(qapp, settings, dispose):
    pages = []

    def build(core=None, show=True):
        page = IntegrationsPage(settings)
        page.resize(1200, 800)
        page.set_core(core or FakeCore())
        if show:
            page.show()
            qapp.processEvents()
        pages.append(page)
        return page

    yield build
    for page in pages:
        dispose(page)


# -- what is asked ------------------------------------------------------------
def test_the_first_look_reads_the_connections_and_one_page_of_the_list(make):
    core = FakeCore()
    page = make(core)
    assert [args[0] for args, _doc in core.calls] == ["list", "call"]
    assert core.asked() == ["search"]
    request = core.calls[1][1]
    assert request["size"] == jira_module.PAGE_ROWS and "cursor" not in request
    assert page.jira.issues.list.count() == 2


def test_coming_back_and_changing_tabs_asks_jira_nothing(make, qapp):
    core = FakeCore()
    page = make(core)
    before = core.asked()
    page.hide()
    page.show()
    for index in (1, 2, 3, 0):
        page.jira.tabs.setCurrentIndex(index)
        qapp.processEvents()
    assert core.asked() == before
    # The Activity tab read the project's own log - a file, not Jira.
    assert [args for args, _doc in core.calls if args[0] == "activity"] == [("activity",)]


def test_opening_an_issue_reads_that_issue_alone(make):
    core = FakeCore()
    page = make(core)
    page.jira.issues.select_key("QA-139")
    assert core.asked() == ["search", "issue"]
    assert core.calls[-1][1] == {"key": "QA-139", "comments": jira_module.COMMENTS}
    assert page.jira.issues.detail.summary.text() == "Session expires"


def test_refresh_is_one_call_for_the_rows_on_screen_and_the_open_issue(make):
    core = FakeCore()
    page = make(core)
    page.jira.issues.select_key("QA-142")
    page.jira.issues.refresh.click()
    assert core.asked() == ["search", "issue", "refresh"]
    request = core.calls[-1][1]
    assert request["list"]["size"] == jira_module.PAGE_ROWS
    assert request["list"].get("cursor") is None
    assert request["issue"] == {"key": "QA-142", "comments": jira_module.COMMENTS}


def test_typing_asks_nothing_and_enter_does(make):
    core = FakeCore()
    page = make(core)
    search = page.jira.issues.search
    search.setText("expires")
    assert core.asked() == ["search"]
    search.returnPressed.emit()
    assert core.asked() == ["search", "search"]
    assert core.calls[-1][1]["text"] == "expires"
    search.clear()
    assert core.asked() == ["search", "search", "search"], \
        "clearing what was searched for asks for the list without it"


def test_a_filter_s_choices_are_read_once_when_its_box_first_opens(make):
    core = FakeCore()
    page = make(core)
    projects = page.jira.issues.projects
    assert not projects.loaded() and core.asked() == ["search"]
    projects.showPopup()
    projects.hidePopup()
    projects.showPopup()
    projects.hidePopup()
    assert core.asked() == ["search", "projects"]
    assert projects.findData("QA") > 0


def test_another_connection_is_not_loaded_until_asked(make):
    core = FakeCore([_connection(), _connection("ops", "Operations")])
    page = make(core)
    view = page.jira
    view.picker.setCurrentIndex(1)
    view.picker.activated.emit(1)
    assert core.asked() == ["search"]
    assert view.issues.blank.currentWidget() is view.issues.start
    assert view.issues.start_title.text() == "Issues of Operations are not loaded"
    view.issues.start_button.click()
    assert core.asked() == ["search", "search"]
    assert core.calls[-1][0][1] == "ops"


def test_load_more_continues_from_the_cursor(make):
    core = FakeCore(search=lambda doc: {"ok": True, "result": {
        "rows": [_row("QA-%d" % (200 if doc.get("cursor") else 1))],
        "cursor": None if doc.get("cursor") else "next"}})
    page = make(core)
    issues = page.jira.issues
    assert issues.load_more.isVisibleTo(page) and issues.count.text() == "1+ ISSUES"
    issues.load_more.click()
    assert core.calls[-1][1]["cursor"] == "next"
    assert [row["key"] for row in issues.rows()] == ["QA-1", "QA-200"]
    assert not issues.load_more.isVisibleTo(page)


def test_earlier_comments_are_read_only_when_asked(make):
    issue = _issue("QA-142", comments={"items": [{"id": str(n), "author": "Olena",
                                                  "created": "", "html": "x"}
                                                 for n in range(20)], "total": 30})
    core = FakeCore(issue={"ok": True, "result": {"issue": issue}})
    page = make(core)
    page.jira.issues.select_key("QA-142")
    detail = page.jira.issues.detail
    assert detail.earlier.isVisibleTo(page)
    assert detail.earlier.text() == "Show earlier comments (10)"
    detail.earlier.click()
    assert core.asked()[-1] == "comments" and core.calls[-1][1]["start"] == 20


# -- what is shown when there is nothing --------------------------------------
def test_no_connection_offers_to_add_one_and_asks_jira_nothing(make):
    core = FakeCore(connections=[])
    page = make(core)
    issues = page.jira.issues
    assert core.asked() == []
    assert issues.start_title.text() == "No Jira connection yet"
    assert issues.start_button.text() == "Add connection"
    assert page.jira.tag.text() == "Not connected"
    assert not page.jira.picker.isEnabled()
    for control in (issues.search, issues.refresh, issues.projects, issues.statuses,
                    issues.mine, issues.jql):
        assert not control.isEnabled()
    # Not a blank box: the picker says there is nothing to pick.
    assert page.jira.picker.currentIndex() == -1
    assert page.jira.picker.placeholderText() == "No connection yet"


def test_a_connection_without_a_token_asks_nothing_until_one_is_set(make):
    core = FakeCore(connections=[_connection(token=False)])
    page = make(core)
    assert core.asked() == []
    assert page.jira.issues.start_title.text() == "Product team has no token"
    assert page.jira.tag.text() == "No token"


def test_a_refused_sign_in_shows_in_the_tag_and_says_what_to_do(make):
    core = FakeCore(search={"ok": False, "kind": "signin", "status": 401,
                            "error": "Jira answered 401"})
    page = make(core)
    issues = page.jira.issues
    assert page.jira.tag.text() == "Sign-in refused"
    assert issues.problem.isVisibleTo(page)
    assert "Jira answered 401" in issues.problem_text.text()
    assert issues.problem_action.text() == "Open Connections"


def test_an_issue_that_is_gone_says_so(make):
    core = FakeCore(issue={"ok": False, "kind": "not_found", "status": 404, "error": "x"})
    page = make(core)
    page.jira.issues.select_key("QA-142")
    detail = page.jira.issues.detail
    assert detail.stack.currentIndex() == 2
    assert detail.gone_title.text() == "QA-142 is not available"


def test_an_empty_list_says_why_it_is_empty(make):
    core = FakeCore(rows=[])
    page = make(core)
    issues = page.jira.issues
    assert issues.note.text() == "No issues were updated in the last 90 days."
    issues.mine.setChecked(True)
    assert issues.note.text() == "No issues match these filters. Clear them to see more."
    assert issues.detail.stack.currentIndex() == 0
    assert issues.detail.empty.isVisibleTo(page)


def test_an_issue_without_a_description_files_or_comments_says_so(make, qapp):
    bare = _issue("QA-142", description_html="", labels=[], assignee="", reporter="")
    page = make(FakeCore(issue={"ok": True, "result": {"issue": bare}}))
    page.jira.issues.select_key("QA-142")
    qapp.processEvents()
    detail = page.jira.issues.detail
    texts = [label.text() for label in detail.findChildren(jira_module.QLabel)
             if label.isVisibleTo(detail)]
    for words in ("No description.", "No attachments.", "No comments yet.", "Unassigned"):
        assert words in texts, words
    assert detail.labels.text() == "—" and detail.reporter.text() == "—"


# -- connections --------------------------------------------------------------
def test_a_new_connection_s_token_goes_in_the_document_never_the_command(make, qapp,
                                                                         dispose):
    core = FakeCore(connections=[])
    page = make(core)
    dialog = ConnectionDialog(page, None)
    try:
        dialog.save_requested.connect(page.jira.save_connection)
        dialog.auth.setCurrentIndex(dialog.auth.findData("basic"))
        dialog.name.setText("Product team")
        dialog.site.setText("https://qa.example.invalid")
        dialog.email.setText("you@yourcompany.example")
        dialog.token.setText(TOKEN)
        dialog.save.click()
        args, document = core.calls[-1]
        assert args == ("save",)
        assert document["token"] == TOKEN
        assert TOKEN not in " ".join(args)
        assert dialog.result() == QDialog.Accepted
    finally:
        dispose(dialog)
    assert page.jira.picker.currentText() == "Product team"


def test_a_new_connection_needs_its_token_and_a_name_of_its_own(make, dispose):
    core = FakeCore()
    page = make(core)
    dialog = ConnectionDialog(page, None, taken=["Product team"])
    try:
        dialog.auth.setCurrentIndex(dialog.auth.findData("basic"))
        dialog.name.setText("Ops")
        dialog.site.setText("https://qa.example.invalid")
        dialog.save.click()
        assert "Type the token" in dialog.problem.text()
        dialog.name.setText("product team")
        dialog.token.setText(TOKEN)
        dialog.save.click()
        assert "already exists" in dialog.problem.text()
        assert [args for args, _doc in core.calls if args[0] == "save"] == []
    finally:
        dispose(dialog)


def test_the_dialog_s_fields_and_pickers_are_one_width(make, qapp, dispose):
    page = make(FakeCore())
    dialog = ConnectionDialog(page, None)
    try:
        dialog.show()
        dialog.auth.setCurrentIndex(dialog.auth.findData("basic"))
        qapp.processEvents()
        widths = {widget.width() for widget in (dialog.name, dialog.product, dialog.site,
                                                dialog.auth, dialog.email, dialog.token)}
        assert len(widths) == 1, widths
        dialog.auth.setCurrentIndex(dialog.auth.findData("oauth"))
        dialog._pending = "pending:x"
        dialog._fill_sites([{"id": "c", "name": "n", "url": "https://n.example.invalid"}])
        dialog._say_oauth()
        qapp.processEvents()
        assert dialog.sites.isVisible()
        assert {dialog.name.width(), dialog.auth.width(), dialog.sites.width()} == \
            {dialog.name.width()}
    finally:
        dispose(dialog)


# -- signing in with Atlassian ------------------------------------------------
def _signing_dialog(page, connection=None, developer=False):
    dialog = ConnectionDialog(page, connection, developer=developer)
    dialog.save_requested.connect(page.jira.save_connection)
    dialog.sign_in_requested.connect(page.jira.sign_in)
    dialog.app_requested.connect(lambda: page.jira.open_app(dialog))
    return dialog


def test_a_new_connection_offers_signing_in_with_atlassian_first(make, dispose):
    page = make(FakeCore(connections=[]))
    dialog = _signing_dialog(page)
    try:
        assert dialog.auth.currentData() == "oauth"
        assert dialog.oauth_box.isVisibleTo(dialog) and not dialog.token_box.isVisibleTo(dialog)
        assert dialog.sign_in.text() == "Sign in with Atlassian"
        dialog.save.click()
        assert dialog.problem.text() == "Sign in with Atlassian first."
    finally:
        dispose(dialog)


def test_signing_in_fills_the_site_and_saves_without_a_token(make, dispose):
    core = FakeCore(connections=[])
    page = make(core)
    dialog = _signing_dialog(page)
    try:
        dialog.sign_in.click()
        assert core.calls[-1] == (("sign-in",), {})
        assert dialog.oauth_state.text() == "Signed in as Alex."
        assert dialog.sites.count() == 1 and dialog.name.text() == "yourcompany"
        dialog.save.click()
        args, document = core.calls[-1]
        assert args == ("save",)
        assert document["oauth_pending"] == "pending:abc" and "token" not in document
        assert document["connection"]["cloud_id"] == "cloud-1"
        assert document["connection"]["site"] == "https://yourcompany.atlassian.net"
        assert document["connection"]["auth"] == "oauth"
        assert dialog.result() == QDialog.Accepted
    finally:
        dispose(dialog)


def test_without_the_oauth_app_signing_in_opens_its_settings(make, dispose, monkeypatch):
    from cms_gui.pages import jiraconnections
    shown = []
    monkeypatch.setattr(jiraconnections.OAuthAppDialog, "exec",
                        lambda self: shown.append(self.client_id.text()) or 0)
    core = FakeCore(connections=[], **{"sign-in": {"ok": False, "kind": "app",
                                                   "error": "Set up the OAuth app first"}})
    page = make(core)
    dialog = _signing_dialog(page, developer=True)
    try:
        dialog.sign_in.click()
        assert "Set up the OAuth app first" in dialog.problem.text()
        assert shown == ["client-1"]
        assert [args[0] for args, _doc in core.calls][-1] == "app"
    finally:
        dispose(dialog)


def test_an_ordinary_user_only_signs_in_and_never_sees_an_app(make, dispose, monkeypatch):
    from cms_gui.pages import jiraconnections
    monkeypatch.setattr(jiraconnections.OAuthAppDialog, "exec",
                        lambda self: pytest.fail("showed a developer's dialog"))
    core = FakeCore(connections=[], **{"sign-in": {
        "ok": False, "kind": "app",
        "error": "This QAVector has no Atlassian sign-in service."}})
    page = make(core)
    dialog = _signing_dialog(page)
    try:
        assert not dialog.app_button.isVisibleTo(dialog)
        assert "OAuth" not in dialog.oauth_state.text()
        dialog.sign_in.click()
        assert dialog.problem.text() == "This QAVector has no Atlassian sign-in service."
    finally:
        dispose(dialog)


def test_developer_mode_offers_the_sign_in_setup(make, dispose):
    page = make(FakeCore())
    page.set_developer_mode(True)
    assert page.jira.connections_tab.developer is True
    dialog = _signing_dialog(page, developer=True)
    try:
        assert dialog.app_button.isVisibleTo(dialog)
    finally:
        dispose(dialog)


def test_signing_in_again_names_the_connection(make, dispose):
    core = FakeCore([_connection(auth="oauth", cloud_id="cloud-1",
                                 site="https://yourcompany.atlassian.net", email="")])
    page = make(core)
    dialog = _signing_dialog(page, page.jira.connections[0])
    try:
        assert dialog.sign_in.text() == "Sign in again"
        dialog.sign_in.click()
        assert core.calls[-1] == (("sign-in",), {"connection": "product-team"})
    finally:
        dispose(dialog)


def test_closing_the_dialog_stops_a_sign_in_that_is_waiting(make, dispose):
    import threading
    page = make(FakeCore())
    dialog = ConnectionDialog(page, None)
    try:
        dialog._cancel = threading.Event()
        dialog.reject()
        assert dialog._cancel.is_set()
    finally:
        dispose(dialog)


def test_the_oauth_app_s_callback_follows_its_port_and_its_secret_is_never_shown(
        make, dispose):
    from cms_gui.pages.jiraconnections import OAuthAppDialog
    page = make(FakeCore())
    dialog = OAuthAppDialog(page, {"client_id": "client-1", "port": 47321,
                                   "secret_set": True})
    try:
        assert dialog.secret.text() == ""
        assert dialog.secret.placeholderText() == "Set - leave empty to keep it"
        dialog.port.setValue(50001)
        assert dialog.callback.text() == "http://localhost:50001/callback"
        assert dialog.document() == {"client_id": "client-1", "secret": "", "port": 50001,
                                     "service": ""}
    finally:
        dispose(dialog)


def test_editing_keeps_the_token_unless_one_is_typed(make, dispose):
    page = make(FakeCore())
    dialog = ConnectionDialog(page, _connection())
    try:
        assert dialog.token.text() == ""
        assert dialog.token.placeholderText() == "Set - leave empty to keep it"
        document = dialog.document()
        assert document["token"] == "" and "token_set" not in document["connection"]
        assert document["connection"]["id"] == "product-team"
    finally:
        dispose(dialog)


def test_removing_a_connection_asks_first_and_names_no_user_of_it(make, monkeypatch):
    asked = []
    monkeypatch.setattr(widgets, "confirm", lambda *args, **kw: asked.append(args) or True)
    core = FakeCore()
    page = make(core)
    tab = page.jira.connections_tab
    assert [tab.table.horizontalHeaderItem(column).text()
            for column in range(tab.table.columnCount())] == list(tab.HEADERS)
    assert "Used by" not in tab.HEADERS
    tab.table.selectRow(0)
    tab.remove()
    assert asked[0][3] == ("Its token is forgotten too, and whatever names this "
                           "connection stops working until it names another. Nothing in "
                           "Jira changes.")
    assert core.calls[-1][0] == ("delete", "product-team")
    assert page.jira.connections == []


def test_a_scoped_token_is_said_in_the_account_column(make):
    page = make(FakeCore([_connection(gateway=True, cloud_id="cloud-7")]))
    assert page.jira.connections_tab.table.item(0, 3).text() == \
        "you@yourcompany.example · scoped token"


def test_test_signs_in_once_and_says_as_whom(make):
    core = FakeCore()
    page = make(core)
    tab = page.jira.connections_tab
    tab.table.selectRow(0)
    tab.test_button.click()
    assert core.asked()[-1] == "test"
    assert tab.message.text() == "Signed in as Alex."
    assert tab.table.item(0, 4).text() == "Connected"


def test_settings_are_saved_with_the_connection_and_change_nothing_else(make):
    core = FakeCore()
    page = make(core)
    tab = page.jira.settings_tab
    assert not tab.save.isEnabled()
    tab.project.setText("qa")
    tab.project.textEdited.emit("qa")
    tab.save.click()
    args, document = core.calls[-1]
    assert args == ("save",) and "token" not in document
    assert document["connection"]["settings"] == {"default_project": "QA",
                                                  "refresh_minutes": 0}


def test_refreshing_on_a_timer_is_off_unless_a_connection_says_so(make, qapp):
    page = make(FakeCore())
    assert not page.jira.auto.isActive()
    timed = make(FakeCore([_connection(settings={"refresh_minutes": 5,
                                                 "default_project": ""})]))
    assert timed.jira.auto.isActive() and timed.jira.auto.interval() == 5 * 60000
    timed.hide()
    assert not timed.jira.auto.isActive()


# -- shape --------------------------------------------------------------------
@pytest.mark.parametrize("which", ["connections", "activity", "catalog"])
def test_every_table_has_a_search_box_over_every_column(make, qapp, which):
    page = make(FakeCore())
    if which == "catalog":
        page.show_catalog()
        table, search = page.table, page.search
    else:
        tab = page.jira.connections_tab if which == "connections" else page.jira.activity
        page.jira.tabs.setCurrentWidget(tab)
        table, search = tab.table, tab.search
    qapp.processEvents()
    header = table.horizontalHeader()
    for column in range(table.columnCount()):
        box = search.box(column)
        assert box is not None and box.isVisible()
        assert box.x() == header.sectionViewportPosition(column)
        assert box.width() == header.sectionSize(column) + 1


def test_the_breadcrumb_leads_to_the_providers_and_back(make, settings):
    page = make(FakeCore())
    assert page.stack.currentWidget() is page.jira
    page.jira.crumb.click()
    assert page.stack.currentWidget() is page.catalog
    assert page.table.item(0, 0).text() == "Jira" and page.table.item(0, 1).text() == "1"
    assert settings.integrations_state()["view"] == ""
    page.open_selected()
    assert page.stack.currentWidget() is page.jira


def test_on_a_narrow_page_the_issue_goes_below_the_list(make, qapp):
    page = make(FakeCore())
    issues = page.jira.issues
    page.resize(700, 700)
    qapp.processEvents()
    assert issues.width() < 760
    assert issues.splitter.orientation() == Qt.Vertical
    page.resize(1200, 700)
    qapp.processEvents()
    assert issues.splitter.orientation() == Qt.Horizontal


def test_the_list_folds_away_under_the_issue_and_comes_back(make, qapp, settings):
    page = make(FakeCore())
    splitter = page.jira.issues.splitter
    assert splitter.isCollapsible(0) and not splitter.isCollapsible(1)
    total = sum(splitter.sizes())
    splitter.setSizes([0, total])
    splitter.splitterMoved.emit(0, 1)
    qapp.processEvents()
    assert splitter.folded() == [0]
    # Remembered, as a person left it.
    again = make(FakeCore())
    assert again.jira.issues.splitter.folded() == [0]
    splitter.unfold(0)
    assert splitter.folded() == [] and splitter.sizes()[0] > 0


def test_filters_and_the_connection_are_remembered_for_the_project(make, settings):
    core = FakeCore([_connection(), _connection("ops", "Operations")])
    page = make(core)
    page.jira.issues.mine.setChecked(True)
    page.jira.picker.setCurrentIndex(1)
    page.jira.picker.activated.emit(1)
    state = settings.integrations_state()
    assert state["connection"] == "ops" and state["filters"]["mine"] is True
    again = make(FakeCore([_connection(), _connection("ops", "Operations")]))
    assert again.jira.picker.currentData() == "ops"
    assert again.jira.issues.mine.isChecked()


def test_a_live_theme_change_repaints_the_tag_and_the_chips(make, qapp):
    page = make(FakeCore())
    try:
        theme.set_dark_mode(True)
        qapp.setStyleSheet(theme.stylesheet())
        qapp.processEvents()
        assert theme.TAG_TINT in page.jira.tag.styleSheet()
    finally:
        theme.set_dark_mode(False)
        qapp.setStyleSheet(theme.stylesheet())


def test_times_are_said_the_way_a_person_says_them():
    import datetime
    now = datetime.datetime(2026, 11, 6, 12, 0, tzinfo=datetime.timezone.utc)
    assert jira_module.ago("2026-11-06T11:59:30.000+0000", now) == "just now"
    assert jira_module.ago("2026-11-06T11:36:00.000+0000", now) == "24 minutes ago"
    assert jira_module.ago("2026-11-06T13:00:00.000+0300", now) == "2 hours ago"
    assert jira_module.ago("2026-11-05T09:00:00.000+0000", now) == "yesterday"
    assert jira_module.ago("not a time", now) == ""
    assert jira_module.initials("Olena Kovalenko") == "OK"
    assert jira_module.initials("") == "?"


# -- changing issues ----------------------------------------------------------
def _opened(make, core=None, **issue):
    core = core or FakeCore(issue={"ok": True, "result": {"issue": _issue("QA-142", **issue)}})
    page = make(core)
    page.jira.issues.select_key("QA-142")
    return core, page, page.jira.issues.detail


def test_the_status_button_offers_the_workflow_s_moves_and_makes_one(make):
    core, page, detail = _opened(make, transitions=[
        {"id": "31", "name": "Done", "to": "Done", "category": "done"},
        {"id": "21", "name": "Send back", "to": "To Do", "category": "new"}])
    menu = detail.status.menu()
    assert [one.text() for one in menu.actions()] == ["Done", "Send back  (to To Do)"]
    assert detail.status.isEnabled()
    menu.actions()[0].trigger()
    assert core.calls[-1] == (("call", "product-team", "transition"),
                              {"key": "QA-142", "to": "31"})
    assert detail.status.text() == "Done" and detail.note.text() == "Moved to Done."
    assert page.jira.issues.rows()[0]["status"] == "Done"


def test_a_move_that_needs_fields_asks_for_them_and_sends_them(make, monkeypatch):
    from cms_gui.pages import jiradialogs
    answers = iter([{"ok": False, "kind": "fields", "transition": "Done", "error": "x",
                     "needs": [{"key": "resolution", "name": "Resolution",
                                "allowed": [{"id": "1", "name": "Fixed"}]}]}, None])
    core = FakeCore(issue={"ok": True, "result": {"issue": _issue("QA-142", transitions=[
        {"id": "31", "name": "Done", "to": "Done", "category": "done"}])}})
    default = FakeCore.integrations

    def integrations(self, *args, document=None, **kwargs):
        if args[:3] == ("call", "product-team", "transition"):
            answer = next(answers)
            if answer is not None:
                self.calls.append((args, document))
                return answer
        return default(self, *args, document=document, **kwargs)

    monkeypatch.setattr(FakeCore, "integrations", integrations)
    monkeypatch.setattr(jiradialogs.TransitionFieldsDialog, "exec",
                        lambda self: QDialog.Accepted)
    _core, page, detail = _opened(make, core)
    detail.status.menu().actions()[0].trigger()
    sent = [doc for args, doc in core.calls if args[-1] == "transition"]
    assert sent == [{"key": "QA-142", "to": "31"},
                    {"key": "QA-142", "to": "31", "fields": {"resolution": "1"}}]
    assert detail.problem.isHidden()


def test_a_comment_is_posted_once_typed_and_the_box_emptied(make):
    core, page, detail = _opened(make)
    assert not detail.post.isEnabled()
    detail.composer.setPlainText("Fixed on staging")
    assert detail.post.isEnabled() and page.jira.has_unsaved()
    detail.post.click()
    assert core.calls[-1] == (("call", "product-team", "comment"), {
        "key": "QA-142", "body": {"type": "doc", "version": 1, "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Fixed on staging"}]}]}})
    assert detail.composer.toPlainText() == "" and not page.jira.has_unsaved()
    assert detail.note.text() == "Comment posted."


# -- editing in place ---------------------------------------------------------
UPDATED = "2026-11-05T10:00:00.000+0000"
CONFLICT = "QA-142 was changed in Jira after you opened it."


def _ops(core):
    return [args[-1] for args, _doc in core.calls if args[0] == "call"]


def test_an_issue_is_read_only_until_edit_is_pressed(make):
    core, page, detail = _opened(make)
    assert detail.summary.isVisible() and not detail.summary_edit.isVisible()
    assert detail.labels.isVisible() and not detail.labels_edit.isVisible()
    assert detail.priority.isVisible() and not detail.priority_edit.isVisible()
    assert not detail.description_edit.isVisible() and not detail.save.isVisible()
    assert "edit_meta" not in _ops(core), "nothing asked until Edit is pressed"
    assert not page.jira.has_unsaved()


def test_edit_turns_the_fields_into_fields_where_they_stand(make):
    core, page, detail = _opened(make)
    detail.edit.click()
    assert _ops(core)[-1] == "edit_meta"
    for shown in (detail.summary_edit, detail.labels_edit, detail.priority_edit,
                  detail.save, detail.cancel):
        assert shown.isVisible()
    for hidden in (detail.summary, detail.labels, detail.priority, detail.status,
                   detail.edit, detail.more):
        assert not hidden.isVisible()
    assert detail.summary_edit.text() == "Session expires"
    assert detail.labels_edit.text() == "regression"
    assert detail.priority_edit.isEnabled() and detail.priority_edit.currentText() == "High"
    assert not detail.description_edit.isVisible(), "the screen does not offer it"
    assert not detail.save.isEnabled(), "nothing changed yet"
    # Nothing else is written to it meanwhile: its own change would look like
    # somebody else's when the edit is saved.
    detail.composer.setPlainText("A note")
    assert not detail.attach.isEnabled() and not detail.post.isEnabled()


def test_save_sends_only_what_changed_and_goes_back_to_reading(make):
    core, page, detail = _opened(make)
    detail.edit.click()
    detail.summary_edit.setText("Session expires after an hour")
    assert detail.save.isEnabled() and page.jira.has_unsaved()
    detail.save.click()
    assert core.calls[-1] == (("call", "product-team", "edit"),
                              {"key": "QA-142", "updated": UPDATED,
                               "fields": {"summary": "Session expires after an hour"}})
    assert detail.editing is None and detail.summary.isVisible()
    assert detail.summary.text() == "Session expires after an hour"
    assert detail.note.text() == "Saved." and detail.status.isVisible()
    assert page.jira.issues.rows()[0]["summary"] == "Session expires after an hour"
    assert not page.jira.has_unsaved()


def test_an_edit_over_somebody_else_s_change_stays_open_and_says_so_under_save(make):
    core = FakeCore(issue={"ok": True, "result": {"issue": _issue("QA-142")}},
                    edit={"ok": False, "kind": "conflict", "error": CONFLICT})
    _core, page, detail = _opened(make, core)
    detail.edit.click()
    detail.summary_edit.setText("Mine")
    detail.save.click()
    assert detail.editing is not None and detail.summary_edit.text() == "Mine"
    assert detail.edit_problem.isVisible() and detail.edit_problem.text() == CONFLICT
    assert detail.problem.isHidden()
    assert detail.save.isEnabled() and detail.save.text() == "Save"
    detail.summary_edit.setText("Mine, again")
    assert detail.edit_problem.isHidden(), "typing again clears the old reason"


def test_refresh_while_editing_keeps_what_was_typed_and_when_editing_began(make):
    newer = "2026-11-06T09:00:00.000+0000"
    core = FakeCore(issue={"ok": True, "result": {"issue": _issue("QA-142")}},
                    refresh=lambda document: {"ok": True, "result": {
                        "list": {"rows": [_row("QA-142"), _row("QA-139")], "cursor": None},
                        "issue": _issue("QA-142", updated=newer)}})
    _core, page, detail = _opened(make, core)
    detail.edit.click()
    detail.summary_edit.setText("Mine")
    page.jira.refresh()
    assert detail.editing is not None and detail.summary_edit.text() == "Mine"
    detail.save.click()
    assert core.calls[-1][1]["updated"] == UPDATED, "Jira decides whether it changed"


def test_a_formatted_description_is_rewritten_only_when_the_person_says_so(make):
    meta = {"ok": True, "result": {"editable": ["summary", "description"], "priorities": []}}
    core = FakeCore(issue={"ok": True, "result": {"issue": _issue(
        "QA-142", description_text="One\nTwo", description_plain=False)}}, edit_meta=meta)
    _core, page, detail = _opened(make, core)
    detail.edit.click()
    assert detail.rich_row.isVisible() and detail.description.isVisible()
    assert not detail.description_edit.isVisible()
    assert "description" not in detail.changes()
    detail.rewrite.click()
    assert detail.description_edit.isVisible() and not detail.description.isVisible()
    assert detail.description_edit.toPlainText() == "One\nTwo"
    detail.description_edit.setPlainText("Rewritten")
    assert detail.changes()["description"] == "Rewritten"
    detail.rewrite.click()
    assert "description" not in detail.changes() and detail.description.isVisible()


def test_a_plain_description_is_typed_in_where_it_is_read(make):
    meta = {"ok": True, "result": {"editable": ["description"], "priorities": []}}
    core = FakeCore(issue={"ok": True, "result": {"issue": _issue(
        "QA-142", description_text="Steps", description_plain=True)}}, edit_meta=meta)
    _core, page, detail = _opened(make, core)
    detail.edit.click()
    assert detail.description_edit.isVisible() and not detail.rich_row.isVisible()
    assert detail.description_edit.toPlainText() == "Steps"
    assert not detail.summary_edit.isEnabled(), "the screen does not offer it"
    detail.description_edit.setPlainText("Steps\n\nand more")
    assert detail.changes() == {"description": "Steps\n\nand more"}


def test_cancel_drops_the_changes_and_sends_nothing(make):
    core, page, detail = _opened(make)
    detail.edit.click()
    detail.labels_edit.setText("regression smoke")
    sent = len(core.calls)
    detail.cancel.click()
    assert len(core.calls) == sent
    assert detail.editing is None and detail.labels.text() == "regression"
    assert not page.jira.has_unsaved()


def test_escape_puts_an_untouched_edit_down_and_asks_about_a_changed_one(make, monkeypatch):
    asked = []
    monkeypatch.setattr(widgets, "confirm", lambda *args, **kw: asked.append(args[2]))
    core, page, detail = _opened(make)
    detail.edit.click()
    assert detail.escape_key.isEnabled()
    detail.escape_key.activated.emit()
    assert detail.editing is None and asked == []
    assert not detail.escape_key.isEnabled() and not detail.save_key.isEnabled()
    detail.edit.click()
    detail.summary_edit.setText("Mine")
    detail.escape_key.activated.emit()
    assert asked == ["Discard your changes to QA-142?"]
    assert detail.editing is not None, "kept: the question was not agreed to"


def test_ctrl_s_saves(make):
    core, page, detail = _opened(make)
    detail.edit.click()
    detail.summary_edit.setText("Mine")
    detail.save_key.activated.emit()
    assert _ops(core)[-1] == "edit" and detail.editing is None


def test_opening_another_issue_mid_edit_asks_and_keeping_it_keeps_its_row(make, monkeypatch):
    answers = []
    monkeypatch.setattr(widgets, "confirm", lambda *args, **kw: answers.pop(0))
    core, page, detail = _opened(make, FakeCore())
    issues = page.jira.issues
    detail.edit.click()
    detail.summary_edit.setText("Mine")
    answers.append(False)
    issues.select_key("QA-139")
    assert detail.editing["key"] == "QA-142" and detail.summary_edit.text() == "Mine"
    assert issues.list.currentItem().data(Qt.UserRole)["key"] == "QA-142"
    assert not [doc for args, doc in core.calls
                if args[-1] == "issue" and doc["key"] == "QA-139"]
    answers.append(True)
    issues.select_key("QA-139")
    assert detail.editing is None and detail.issue["key"] == "QA-139"


def test_an_untouched_edit_is_put_down_without_a_question(make, monkeypatch):
    monkeypatch.setattr(widgets, "confirm", lambda *args, **kw: pytest.fail("asked"))
    core, page, detail = _opened(make, FakeCore())
    detail.edit.click()
    page.jira.issues.select_key("QA-139")
    assert detail.editing is None and detail.issue["key"] == "QA-139"


def test_another_connection_mid_edit_asks_and_keeping_it_puts_the_picker_back(
        make, monkeypatch):
    monkeypatch.setattr(widgets, "confirm", lambda *args, **kw: False)
    core = FakeCore(connections=[_connection(), _connection("ops", "Ops")],
                    issue={"ok": True, "result": {"issue": _issue("QA-142")}})
    _core, page, detail = _opened(make, core)
    detail.edit.click()
    detail.summary_edit.setText("Mine")
    picker = page.jira.picker
    picker.setCurrentIndex(picker.findData("ops"))
    page.jira._connection_picked(picker.currentIndex())
    assert picker.currentData() == "product-team"
    assert detail.editing is not None and detail.summary_edit.text() == "Mine"


def test_edit_keeps_the_place_on_the_page_and_starts_in_the_summary(make, qapp):
    long_text = "".join("<p>line %d</p>" % index for index in range(60))
    core = FakeCore(issue={"ok": True, "result": {"issue": _issue(
        "QA-142", description_html=long_text)}})
    _core, page, detail = _opened(make, core)
    page.activateWindow()
    qapp.processEvents()
    bar = detail.scroll.verticalScrollBar()
    assert bar.maximum() > 0
    detail.edit.setFocus()
    qapp.processEvents()
    assert detail.edit.hasFocus(), "the jump came from the focused Edit being hidden"
    detail.edit.click()
    qapp.processEvents()
    assert bar.value() == 0, "Edit is at the top; the page stays there"
    assert detail.summary_edit.hasFocus()
    detail.summary_edit.setText("Mine")
    detail.save.click()
    qapp.processEvents()
    assert bar.value() == 0


CLOUD_DOC = {"type": "doc", "version": 1, "content": [
    {"type": "paragraph", "content": [{"type": "text", "text": "Steps",
                                       "marks": [{"type": "strong"}]}]},
    {"type": "bulletList", "content": [{"type": "listItem", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "one"}]}]}]}]}


def _cloud(lost=()):
    meta = {"ok": True, "result": {"editable": ["summary", "description"], "priorities": []}}
    return FakeCore(issue={"ok": True, "result": {"issue": _issue(
        "QA-142", description_doc=CLOUD_DOC, description_lost=list(lost),
        description_text="Steps\n- one", description_plain=False)}}, edit_meta=meta)


def test_a_cloud_description_is_edited_with_its_formatting_and_sent_as_a_document(make):
    core = _cloud()
    _core, page, detail = _opened(make, core)
    detail.edit.click()
    editor = detail.rich_edit
    assert editor.isVisible() and not detail.description_edit.isVisible()
    assert not detail.rich_row.isVisible(), "nothing would be lost"
    assert editor.document() == CLOUD_DOC
    assert "description" not in detail.changes(), "opened, not changed"
    cursor = editor.text.textCursor()
    cursor.movePosition(cursor.MoveOperation.End)
    editor.text.setTextCursor(cursor)
    editor.text.insertPlainText(" more")
    assert detail.save.isEnabled()
    detail.save.click()
    sent = core.calls[-1][1]["fields"]["description"]
    assert sent["content"][1]["content"][0]["content"][0]["content"] == [
        {"type": "text", "text": "one more"}]
    assert sent["content"][0] == CLOUD_DOC["content"][0], "the bold stays bold"


def test_what_the_editor_cannot_keep_is_named_and_kept_until_asked(make):
    core = _cloud(lost=["pictures", "tables"])
    _core, page, detail = _opened(make, core)
    detail.edit.click()
    assert detail.rich_row.isVisible() and not detail.rich_edit.isVisible()
    assert "has pictures and tables that" in detail.rich_note.text()
    assert detail.rewrite.text() == "Edit without them"
    detail.rewrite.click()
    assert detail.rich_edit.isVisible() and detail.rewrite.text() == "Keep it as it is"
    detail.rich_edit.text.insertPlainText("x")
    assert "description" in detail.changes()
    detail.rewrite.click()
    assert "description" not in detail.changes() and not detail.rich_edit.isVisible()


def test_people_are_read_only_when_the_assignee_list_is_opened(make):
    core, page, detail = _opened(make)
    detail.edit.click()
    picker = detail.assignee_edit
    assert picker.isVisible() and not detail.assignee.isVisible()
    assert [picker.itemText(index) for index in range(picker.count())] == [
        "Alex", "Unassigned", "Me"]
    assert "assignable" not in _ops(core)
    picker.showPopup()
    assert core.calls[-1] == (("call", "product-team", "assignable"),
                              {"key": "QA-142", "query": ""})
    picker.hidePopup()
    assert [picker.itemText(index) for index in range(picker.count())] == [
        "Alex", "Unassigned", "Me", "Olena", "Maksym"]
    picker.showPopup()
    assert _ops(core).count("assignable") == 1, "read once per edit"
    picker.hidePopup()


def test_a_typed_name_and_enter_looks_for_that_person(make):
    core, page, detail = _opened(make)
    detail.edit.click()
    picker = detail.assignee_edit
    picker.lineEdit().setText("mak")
    picker.lineEdit().returnPressed.emit()
    assert core.calls[-1][1] == {"key": "QA-142", "query": "mak"}
    picker.hidePopup()
    assert picker.findText("Maksym") >= 0 and picker.findText("Olena") < 0


def test_a_new_assignee_is_saved_with_the_edit(make):
    core, page, detail = _opened(make)
    detail.edit.click()
    picker = detail.assignee_edit
    assert detail.changes() == {}
    picker.setCurrentIndex(picker.findData("me"))
    assert detail.changes() == {"assignee": "me"} and detail.save.isEnabled()
    picker.setCurrentIndex(picker.findData(""))
    detail.save.click()
    assert core.calls[-1] == (("call", "product-team", "edit"), {
        "key": "QA-142", "updated": UPDATED, "fields": {"assignee": ""}})


# -- mentions -----------------------------------------------------------------
def _type_in(qapp, editor, text):
    from PySide6.QtTest import QTest
    editor.text.setFocus()
    qapp.processEvents()
    QTest.keyClicks(editor.text, text)
    qapp.processEvents()


def _listed(editor):
    shown = editor.text.mentions
    return [shown.item(index).text() for index in range(shown.count())] \
        if shown.isVisible() else None


def _comment_body(core):
    return [doc for args, doc in core.calls if args[-1] == "comment"][-1]["body"]


def test_the_first_at_sign_reads_who_can_see_the_issue_once(make, qapp):
    core, page, detail = _opened(make, FakeCore())
    page.activateWindow()
    _type_in(qapp, detail.composer, "Hi @")
    assert core.calls[-1] == (("call", "product-team", "mentionable"),
                              {"key": "QA-142", "query": ""})
    assert _listed(detail.composer) == ["Maksym", "Olena"]
    _type_in(qapp, detail.composer, "ol")
    assert _listed(detail.composer) == ["Olena"], "narrowed here, not asked again"
    _type_in(qapp, detail.composer, " and @")
    assert _ops(core).count("mentionable") == 1


def test_a_mention_is_one_piece_of_the_comment_and_is_posted_as_one(make, qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    core, page, detail = _opened(make, FakeCore())
    page.activateWindow()
    _type_in(qapp, detail.composer, "@ma")
    QTest.keyClick(detail.composer.text, Qt.Key_Return)
    assert _listed(detail.composer) is None
    _type_in(qapp, detail.composer, "please look")
    detail.post.click()
    assert _comment_body(core)["content"] == [{"type": "paragraph", "content": [
        {"type": "mention", "attrs": {"id": "acc-maksym", "text": "@Maksym"}},
        {"type": "text", "text": " please look"}]}]


def test_backspace_takes_a_whole_mention(make, qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    core, page, detail = _opened(make, FakeCore())
    page.activateWindow()
    _type_in(qapp, detail.composer, "to @ol")
    QTest.keyClick(detail.composer.text, Qt.Key_Tab)
    QTest.keyClick(detail.composer.text, Qt.Key_Backspace)
    QTest.keyClick(detail.composer.text, Qt.Key_Backspace)
    assert detail.composer.toPlainText() == "to "


def test_a_name_not_among_those_read_is_searched_for_only_when_asked(make, qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    core, page, detail = _opened(make, FakeCore())
    page.activateWindow()
    _type_in(qapp, detail.composer, "@taras")
    assert _listed(detail.composer) == ['Search Jira for "taras"']
    assert _ops(core).count("mentionable") == 1, "typing asks nothing"
    QTest.keyClick(detail.composer.text, Qt.Key_Return)
    assert core.calls[-1][1] == {"key": "QA-142", "query": "taras"}
    assert _listed(detail.composer) == ["Taras"]


def test_escape_closes_the_list_and_not_the_edit(make, qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    meta = {"ok": True, "result": {"editable": ["description"], "priorities": []}}
    core = FakeCore(issue={"ok": True, "result": {"issue": _issue(
        "QA-142", description_doc={"type": "doc", "version": 1, "content": []},
        description_lost=[])}}, edit_meta=meta)
    _core, page, detail = _opened(make, core)
    page.activateWindow()
    detail.edit.click()
    _type_in(qapp, detail.rich_edit, "@")
    assert _listed(detail.rich_edit) == ["Maksym", "Olena"]
    QTest.keyClick(detail.rich_edit.text, Qt.Key_Escape)
    assert _listed(detail.rich_edit) is None and detail.editing is not None


def test_the_description_and_the_comment_share_one_read(make, qapp):
    meta = {"ok": True, "result": {"editable": ["description"], "priorities": []}}
    core = FakeCore(issue={"ok": True, "result": {"issue": _issue(
        "QA-142", description_doc={"type": "doc", "version": 1, "content": []},
        description_lost=[])}}, edit_meta=meta)
    _core, page, detail = _opened(make, core)
    page.activateWindow()
    _type_in(qapp, detail.composer, "@")
    detail.edit.click()
    _type_in(qapp, detail.rich_edit, "@")
    assert _listed(detail.rich_edit) == ["Maksym", "Olena"]
    assert _ops(core).count("mentionable") == 1


def test_another_issue_has_its_own_people(make, qapp):
    core, page, detail = _opened(make, FakeCore())
    page.activateWindow()
    _type_in(qapp, detail.composer, "@")
    detail.composer.clear()
    page.jira.issues.select_key("QA-139")
    _type_in(qapp, detail.composer, "@")
    assert [doc["key"] for args, doc in core.calls if args[-1] == "mentionable"] == [
        "QA-142", "QA-139"]


def test_on_server_a_mention_is_written_into_the_text(make, qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    core = FakeCore(connections=[_connection(flavour="server", auth="bearer")])
    _core, page, detail = _opened(make, core)
    page.activateWindow()
    assert not detail.composer.bar.isVisible()
    _type_in(qapp, detail.composer, "@ol")
    QTest.keyClick(detail.composer.text, Qt.Key_Return)
    _type_in(qapp, detail.composer, "see")
    detail.post.click()
    assert _comment_body(core) == "[~acc-olena] see"


class SlowCore(FakeCore):
    """Answers on a thread, the people a moment later: as a real core does."""

    offload = True

    def integrations(self, *args, **kw):
        import time
        if args and args[-1] == "mentionable":
            time.sleep(0.6)
        return super().integrations(*args, **kw)


def test_the_window_keeps_going_while_people_are_read(make, qapp):
    """The read is on a thread: keys are taken and the event loop turns
    while it is out, and the list says it is loading until the answer."""
    import time
    from PySide6.QtCore import QTimer
    core = SlowCore()
    page = make(core)
    deadline = time.time() + 5
    while not page.jira.issues.rows() and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    page.jira.issues.select_key("QA-142")
    detail = page.jira.issues.detail
    while (detail.issue or {}).get("description_html") is None and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    page.activateWindow()
    _type_in(qapp, detail.composer, "@")
    assert _listed(detail.composer) == ["Loading people…"]
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start(10)
    _type_in(qapp, detail.composer, "ma")
    assert detail.composer.toPlainText() == "@ma", "typed while the read was out"
    assert _listed(detail.composer) == ["Loading people…"]
    while _listed(detail.composer) == ["Loading people…"] and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    timer.stop()
    assert _listed(detail.composer) == ["Maksym"]
    gaps = [later - earlier for earlier, later in zip(ticks, ticks[1:])]
    assert gaps and max(gaps) < 0.15, "the window never stopped for the read"


def test_assigning_to_me_and_unassigning_from_the_more_menu(make):
    core, page, detail = _opened(make)
    detail.assign_me.trigger()
    assert core.calls[-1] == (("call", "product-team", "assign"),
                              {"key": "QA-142", "account": "me"})
    detail.unassign.trigger()
    assert core.calls[-1][1] == {"key": "QA-142", "account": ""}


def test_a_file_is_attached_from_where_the_person_points(make, monkeypatch, tmp_path):
    shot = tmp_path / "shot.png"
    monkeypatch.setattr(widgets, "pick_path", lambda *a, **k: str(shot))
    core, page, detail = _opened(make)
    detail.attach.click()
    assert core.calls[-1] == (("call", "product-team", "attach"),
                              {"key": "QA-142", "path": str(shot)})
    assert detail.note.text() == "File attached."


def test_a_file_is_saved_where_the_person_says(make, monkeypatch, tmp_path, qapp):
    target = tmp_path / "saved.png"
    monkeypatch.setattr(widgets, "pick_path", lambda *a, **k: str(target))
    core, page, detail = _opened(make, attachments=[
        {"id": "10", "filename": "empty-state.png", "size": 2048,
         "url": "https://qa.example.invalid/secure/attachment/10/empty-state.png"}])
    qapp.processEvents()
    save = [button for button in detail.findChildren(QPushButton)
            if button.text() == "Save…" and button.isVisibleTo(detail)]
    assert len(save) == 1
    save[0].click()
    assert core.calls[-1] == (("call", "product-team", "download"), {
        "key": "QA-142", "id": "10", "to": str(target),
        "url": "https://qa.example.invalid/secure/attachment/10/empty-state.png"})
    assert detail.note.text() == "Saved to %s." % target


def test_creating_an_issue_asks_step_by_step_and_opens_the_new_one(make, monkeypatch):
    from cms_gui.pages import jiradialogs

    def drive(dialog):
        dialog.project.setCurrentIndex(dialog.project.findData("QA"))
        assert dialog.type.currentData() == "10001", "one type is chosen for you"
        dialog.inputs["summary"][0].setText("Empty result message")
        dialog.create.click()
        return dialog.result()

    monkeypatch.setattr(jiradialogs.CreateIssueDialog, "exec", drive)
    core = FakeCore()
    page = make(core)
    page.jira.issues.create.click()
    ops = [args[-1] for args, _doc in core.calls if args[0] == "call"]
    assert ops == ["search", "projects", "create_meta", "create_meta", "create"]
    sent = core.calls[-1][1]
    assert sent["project"] == "QA" and sent["type"] == "10001"
    assert sent["fields"]["summary"] == "Empty result message"
    rows = page.jira.issues.rows()
    assert rows[0]["key"] == "QA-200"
    assert page.jira.issues.detail.issue["key"] == "QA-200"


def test_a_change_jira_refuses_is_said_and_changes_nothing(make):
    core = FakeCore(issue={"ok": True, "result": {"issue": _issue("QA-142")}},
                    comment={"ok": False, "kind": "forbidden", "status": 403,
                             "error": "Jira answered 403 - the account may not do that."})
    _core, page, detail = _opened(make, core)
    detail.composer.setPlainText("Hello")
    detail.post.click()
    assert detail.problem.text() == "Jira answered 403 - the account may not do that."
    assert detail.composer.toPlainText() == "Hello", "what was typed is kept"
    assert page.jira.tag.text() == "No access"


def test_the_activity_tab_shows_what_was_changed_and_who_said_they_did_it(make, qapp):
    rows = [{"at": "2026-11-07T10:05:00+0000", "connection": "product-team",
             "op": "transition", "target": "QA-1", "ok": True,
             "summary": "jira: QA-1 To Do -> In Progress",
             "source": {"label": "Cycle pull · step move", "detail": "Run r-1"}},
            {"at": "2026-11-07T10:00:00+0000", "connection": "product-team",
             "op": "comment", "target": "QA-2", "ok": False, "summary": "403",
             "source": {"label": "This page"}}]
    page = make(FakeCore(activity_rows=rows))
    page.jira.tabs.setCurrentWidget(page.jira.activity)
    qapp.processEvents()
    table = page.jira.activity.table
    assert [table.item(0, column).text() for column in range(6)] == [
        "2026-11-07 10:05", "Product team", "Status", "QA-1",
        "Done - jira: QA-1 To Do -> In Progress", "Cycle pull · step move"]
    assert table.item(1, 4).text() == "Failed - 403" and table.item(1, 5).text() == "This page"
    assert table.item(0, 5).toolTip() == "Run r-1"



# -- Ctrl+K -------------------------------------------------------------------
def test_ctrl_k_finds_connections_and_loaded_issues_without_asking_jira(make):
    core = FakeCore()
    page = make(core)
    before = core.asked()
    hits = page.search_rows()
    assert core.asked() == before
    titles = [hit.title for hit in hits]
    assert titles == ["Product team", "QA-142  Session expires", "QA-139  Session expires"]
    assert hits[1].detail == "Jira issue · Product team" and hits[1].status == "In Progress"
    assert hits[1].target == ("jira", ("issue", "product-team", "QA-142"))


def test_a_found_issue_opens_as_a_click_on_it_would(make):
    core = FakeCore()
    page = make(core)
    page.jira.tabs.setCurrentWidget(page.jira.connections_tab)
    page.show_catalog()
    assert page.show_found(("jira", ("issue", "product-team", "QA-139"))) is True
    assert page.stack.currentWidget() is page.jira
    assert page.jira.tabs.currentWidget() is page.jira.issues
    assert core.calls[-1] == (("call", "product-team", "issue"),
                              {"key": "QA-139", "comments": jira_module.COMMENTS})
    assert page.show_found(("jira", ("issue", "product-team", "QA-999"))) is False
    assert page.show_found(("issue", "product-team", "QA-139")) is False, "whose?"


def test_a_found_connection_is_selected_on_its_tab(make):
    page = make(FakeCore())
    assert page.show_found(("jira", ("connection", "product-team"))) is True
    tab = page.jira.connections_tab
    assert page.jira.tabs.currentWidget() is tab and tab.selected()["id"] == "product-team"
    assert page.show_found(("jira", ("connection", "nowhere"))) is False


def test_jira_is_offered_to_search_only_with_a_connection_that_has_a_token(make):
    from cms_gui import search
    for connections in ([], [_connection(token=False)]):
        page = make(FakeCore(connections=connections))
        assert page.search_sources() == []
    core = FakeCore(connections=[_connection(), _connection("ops", "Ops", token=False)])
    page = make(core)
    before = core.asked()
    assert page.search_sources() == [search.Source("", "jira", "Jira",
                                                   "Issues in Product team")]
    assert core.asked() == before, "offering it asks Jira nothing"


def test_a_page_never_opened_reads_its_connections_when_search_first_asks(make):
    core = FakeCore()
    page = make(core, show=False)
    assert [args[0] for args, _doc in core.calls] == []
    assert page.search_sources() == [], "not read yet"
    assert [args[0] for args, _doc in core.calls] == ["list"]
    assert [one.title for one in page.search_sources()] == ["Jira"]
    assert core.asked() == []


def _found_in_jira(document):
    """Ctrl+K's search finds QA-500; the page's own list is the usual two."""
    if not document.get("everywhere"):
        return {"ok": True, "result": {"rows": [_row("QA-142"), _row("QA-139")],
                                       "cursor": None}}
    assert document["size"] == jira_module.FOUND_ROWS
    return {"ok": True, "result": {"rows": [_row("QA-500", "Export fails", "To Do", "new")],
                                   "cursor": None}}


def test_searching_jira_from_ctrl_k_is_one_request_per_connection(make):
    core = FakeCore(connections=[_connection(), _connection("ops", "Ops")],
                    search=_found_in_jira)
    page = make(core)
    sent = len(core.calls)
    got = []
    page.search_source("jira", "qa-500", lambda hits, error=None: got.append((hits, error)))
    asked = [(args[1], doc["text"]) for args, doc in core.calls[sent:]]
    assert asked == [("product-team", "qa-500"), ("ops", "qa-500")]
    (hits, error), = got
    assert error in (None, "")
    assert [(hit.title, hit.detail) for hit in hits] == [
        ("QA-500  Export fails", "Jira issue · Product team"),
        ("QA-500  Export fails", "Jira issue · Ops")]
    assert hits[0].target == ("jira", ("issue", "product-team", "QA-500"))


def test_a_found_issue_opens_even_when_the_list_does_not_have_it(make):
    core = FakeCore(search=_found_in_jira)
    page = make(core, show=False)
    page.search_sources()
    got = []
    page.search_source("jira", "qa-500", lambda hits, error=None: got.append(hits))
    page.show()
    assert page.show_found(got[0][0].target) is True
    assert page.jira.tabs.currentWidget() is page.jira.issues
    assert page.jira.issues.rows()[0]["key"] == "QA-500"
    assert core.calls[-1] == (("call", "product-team", "issue"),
                              {"key": "QA-500", "comments": jira_module.COMMENTS})


def test_a_failed_search_says_why(make):
    core = FakeCore(search={"ok": False, "kind": "signin", "error": "Sign-in refused."})
    page = make(core)
    got = []
    page.search_source("jira", "qa-500", lambda hits, error=None: got.append((hits, error)))
    assert got == [([], "Sign-in refused.")]


# -- every provider can live in an application-owned window ------------------
def test_detach_and_return_preserve_the_same_view_draft_and_loaded_state(make, qapp):
    page = make()
    view = page.jira
    view.open_issue(_row('QA-142'))
    view.issues.detail.composer.setPlainText('Keep this draft')
    view.issues.search.setText('A query not submitted yet')
    before = list(view.core.calls)
    item = view.issues.list.item(0)
    window = page.detach_provider('jira')
    qapp.processEvents()
    assert view.window() is window and view.isVisible()
    assert not window.testAttribute(Qt.WA_QuitOnClose)
    assert window.parent() is None
    assert page.detach_provider('jira') is window
    assert view.issues.list.item(0) is item  # not repopulated on reparenting
    assert view.issues.detail.composer.toPlainText() == 'Keep this draft'
    window.close()
    qapp.processEvents()
    assert page.hosts['jira'].window is None
    assert page.stack.currentWidget() is view
    assert view.issues.detail.issue['key'] == 'QA-142'
    assert view.issues.detail.composer.toPlainText() == 'Keep this draft'
    assert view.issues.search.text() == 'A query not submitted yet'
    assert view.core.calls == before


def test_detached_view_stays_visible_when_main_page_is_hidden(make, qapp):
    page = make()
    window = page.detach_provider('jira')
    page.hide()
    qapp.processEvents()
    assert window.isVisible() and page.jira.isVisible()
    assert page.jira.ticker.isActive()
    page.shutdown()
    assert not window.isVisible()
    assert not page.jira.ticker.isActive()
    assert not page.jira.auto.isActive()


def test_search_and_repeated_open_raise_existing_window_without_docking(make):
    page = make()
    window = page.detach_provider('jira')
    window.showMinimized()
    page.show_catalog()
    assert page.show_found(('jira', ('connection', 'product-team')))
    assert page.hosts['jira'].window is window
    assert not window.isMinimized()
    assert page.jira.tabs.currentWidget() is page.jira.connections_tab
    page.open_provider('jira')
    assert page.jira.window() is window


def test_active_project_changes_in_the_existing_window_and_clears_old_data(make, settings):
    page = make()
    page.set_project_name('Shop')
    window = page.detach_provider('jira')
    page.jira.open_issue(_row('QA-142'))
    page.jira.issues.detail.composer.setPlainText('Discarded by project switch confirmation')
    page.jira.issues.search.setText('Old search')
    page.jira.activity.set_rows([{'op': 'edit', 'target': 'QA-142', 'ok': True}])
    new_core = FakeCore(connections=[_connection('support', 'Support')], rows=[_row('OPS-1')])
    page.set_project_name('Support')
    page.set_core(new_core)
    assert page.hosts['jira'].window is window
    assert 'Support' in window.windowTitle() and 'Shop' not in window.windowTitle()
    assert page.jira.current()['id'] == 'support'
    assert page.jira.issues.list.item(0).data(Qt.UserRole)['key'] == 'OPS-1'
    assert page.jira.issues.detail.issue is None
    assert not page.has_unsaved()
    assert page.jira.issues.search.text() == ''
    assert page.jira.activity.table.rowCount() == 0
    assert new_core.asked() == ['search']


def test_switch_while_connections_load_drops_old_reply_and_starts_new_load(make, monkeypatch):
    pending = []
    monkeypatch.setattr(jira_module.background, 'run',
                        lambda work, done, owner=None: pending.append((work, done)))
    first = FakeCore()
    first.offload = True
    page = make(first)
    assert len(pending) == 1 and page.jira._listing
    old_work, old_done = pending[0]
    # The old request completed before switching, but its UI reply is queued.
    old_result = old_work()
    second = FakeCore(connections=[_connection('second', 'Second')], rows=[])
    page.set_core(second)
    old_done(old_result, None)
    assert page.jira.current()['id'] == 'second'
    assert page.jira.core is second and not page.jira._listing
    assert page.jira._pending == {}


def test_shutdown_cancels_oauth_and_pending_reads_without_delivering_callbacks(make, monkeypatch):
    import threading
    pending = []
    page = make()
    core = page.jira.core
    core.offload = True
    monkeypatch.setattr(jira_module.background, 'run',
                        lambda work, done, owner=None: pending.append((work, done)))
    cancelled = threading.Event()
    answers = []
    page.jira.sign_in({}, answers.append, cancelled)
    page.jira.refresh()
    page.detach_provider('jira')
    before = len(core.calls)
    page.shutdown()
    assert cancelled.is_set()
    for work, done in pending:
        done(work(), None)
    assert len(core.calls) == before
    assert not answers
    assert not page.jira._pending


def test_request_keeps_original_core_even_if_worker_starts_after_context_changed(make, monkeypatch):
    pending = []
    page = make()
    original = page.jira.core
    original.offload = True
    monkeypatch.setattr(jira_module.background, 'run',
                        lambda work, done, owner=None: pending.append((work, done)))
    page.jira.refresh()
    cancel = next(iter(page.jira._pending))
    replacement = FakeCore()
    # Simulate a queued worker starting during context transition. Its captured
    # core must not be resolved dynamically from the page.
    page.jira.core = replacement
    pending[0][0]()
    assert original.asked()[-1] == 'refresh'
    assert replacement.calls == []
    page.shutdown()
    assert cancel.is_set()


def test_write_in_flight_blocks_project_switch_until_the_result_arrives(make, monkeypatch):
    pending = []
    page = make()
    page.jira.open_issue(_row('QA-142'))
    page.jira.core.offload = True
    monkeypatch.setattr(jira_module.background, 'run',
                        lambda work, done, owner=None: pending.append((work, done)))
    page.jira.assign_issue('me')
    assert 'being saved' in page.busy_reason()
    work, done = pending.pop()
    done(work(), None)
    assert not page.busy_reason()


def test_window_geometry_is_reused_across_projects(make):
    page = make()
    first = page.detach_provider('jira')
    first.resize(760, 740)
    page.hosts['jira'].dock()
    page.set_project_name('Another project')
    second = page.detach_provider('jira')
    assert (second.width(), second.height()) == (760, 740)


def test_page_shutdown_cannot_reopen_an_integration_window(make):
    page = make()
    window = page.detach_provider('jira')
    page.shutdown()
    assert not window.isVisible()
    assert page.detach_provider('jira') is None


def test_discarded_settings_do_not_stay_dirty_in_a_project_without_connections(make):
    page = make()
    tab = page.jira.settings_tab
    tab.project.setText('CHANGED')
    tab.project.textEdited.emit('CHANGED')
    assert page.has_unsaved()
    page.set_core(FakeCore(connections=[]))
    assert not page.has_unsaved()
    assert page.jira.connections_tab.table.rowCount() == 0


def test_a_write_is_never_cancelled_and_its_late_answer_is_dropped(make, monkeypatch):
    """Killed half way, nobody could say whether Jira took a change. So a new
    context - another project, new settings, the application quitting - lets
    a write finish; only its answer, now about a context that is gone, is
    dropped."""
    pending = []
    page = make()
    page.jira.open_issue(_row('QA-142'))
    old = page.jira.core
    old.offload = True
    monkeypatch.setattr(jira_module.background, 'run',
                        lambda work, done, owner=None: pending.append((work, done)))
    given = []
    real = FakeCore.integrations

    def integrations(self, *args, document=None, cancel=None, **kwargs):
        given.append((args[-1], cancel))
        return real(self, *args, document=document, **kwargs)

    monkeypatch.setattr(FakeCore, 'integrations', integrations)
    page.jira.assign_issue('me')
    work, done = pending[-1]
    page.set_core(FakeCore())
    page.shutdown()
    done(work(), None)
    assert ('assign', None) in given, 'the write ran, with nothing that could stop it'
    assert page.jira.issues.detail.issue is None, 'its answer belonged to the old project'
