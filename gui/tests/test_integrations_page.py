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
    assert core.calls[-1] == (("call", "product-team", "comment"),
                              {"key": "QA-142", "body": "Fixed on staging"})
    assert detail.composer.toPlainText() == "" and not page.jira.has_unsaved()
    assert detail.note.text() == "Comment posted."


def test_editing_reads_what_may_change_and_sends_only_what_did(make, monkeypatch):
    from cms_gui.pages import jiradialogs

    def drive(dialog):
        assert dialog.priority.isEnabled() and dialog.priority.currentText() == "High"
        assert not dialog.description.isEnabled(), "the screen does not offer it"
        dialog.summary.setText("Session expires after an hour")
        dialog.save.click()
        return dialog.result()

    monkeypatch.setattr(jiradialogs.EditIssueDialog, "exec", drive)
    core, page, detail = _opened(make)
    detail.edit.click()
    ops = [args[-1] for args, _doc in core.calls if args[0] == "call"]
    assert ops[-2:] == ["edit_meta", "edit"]
    assert core.calls[-1][1] == {"key": "QA-142", "updated": "2026-11-05T10:00:00.000+0000",
                                 "fields": {"summary": "Session expires after an hour"}}
    assert detail.summary.text() == "Session expires after an hour"


def test_an_edit_over_somebody_else_s_change_stays_open_and_says_so(make, monkeypatch):
    from cms_gui.pages import jiradialogs
    seen = []

    def drive(dialog):
        dialog.summary.setText("Mine")
        dialog.save.click()
        seen.append((dialog.result(), dialog.problem.text()))
        return dialog.result()

    monkeypatch.setattr(jiradialogs.EditIssueDialog, "exec", drive)
    core = FakeCore(issue={"ok": True, "result": {"issue": _issue("QA-142")}},
                    edit={"ok": False, "kind": "conflict",
                          "error": "QA-142 was changed in Jira after you opened it."})
    _core, page, detail = _opened(make, core)
    detail.edit.click()
    assert seen == [(0, "QA-142 was changed in Jira after you opened it.")]
    assert detail.summary.text() == "Session expires"


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



def test_a_formatted_description_is_replaced_only_when_the_person_says_so(make, dispose):
    from cms_gui.pages.jiradialogs import EditIssueDialog
    page = make(FakeCore())
    dialog = EditIssueDialog(page, _issue("QA-142", description_text="One\nTwo",
                                          description_plain=False))
    try:
        dialog.set_meta({"editable": ["summary", "description"], "priorities": []})
        assert dialog.rich_note.isVisibleTo(dialog) and not dialog.description.isEnabled()
        dialog.description.setPlainText("Rewritten")
        assert "description" not in dialog.changes()
        dialog.rich.setChecked(True)
        assert dialog.changes()["description"] == "Rewritten"
    finally:
        dispose(dialog)


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
    assert hits[1].target == ("issue", "product-team", "QA-142")


def test_a_found_issue_opens_as_a_click_on_it_would(make):
    core = FakeCore()
    page = make(core)
    page.jira.tabs.setCurrentWidget(page.jira.connections_tab)
    page.show_catalog()
    assert page.show_found(("issue", "product-team", "QA-139")) is True
    assert page.stack.currentWidget() is page.jira
    assert page.jira.tabs.currentWidget() is page.jira.issues
    assert core.calls[-1] == (("call", "product-team", "issue"),
                              {"key": "QA-139", "comments": jira_module.COMMENTS})
    assert page.show_found(("issue", "product-team", "QA-999")) is False


def test_a_found_connection_is_selected_on_its_tab(make):
    page = make(FakeCore())
    assert page.show_found(("connection", "product-team")) is True
    tab = page.jira.connections_tab
    assert page.jira.tabs.currentWidget() is tab and tab.selected()["id"] == "product-team"
    assert page.show_found(("connection", "nowhere")) is False
