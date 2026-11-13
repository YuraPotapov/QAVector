"""Integrations -> Agents: what the page shows, and what it asks of the core.

The rule the page is held to is the one Jira's is: nothing is asked unless a
person did something that asks. Showing it reads the connections file; only
Test, Check and Sign in go near an agent's program, and no model is ever asked
anything here. So the fake core writes every call down, and most tests are
about what was called.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QSettings
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QDialog

from cms_gui import theme, widgets
from cms_gui.pages import agents as agents_module
from cms_gui.pages.agents import ConnectionDialog
from cms_gui.pages.integrations import IntegrationsPage
from cms_gui.settings import Settings

KINDS = [{"kind": "claude", "name": "Claude Code", "default_binary": "claude",
          "found": "/home/someone/.local/bin/claude",
          "efforts": ["low", "medium", "high", "xhigh", "max"],
          "modes": ["read", "edit", "full"]}]


def _connection(connection_id="claude", name="Claude", **extra):
    return dict({"id": connection_id, "name": name, "kind": "claude", "binary": "",
                 "model": "", "effort": ""}, **extra)


def _turn(**extra):
    return dict({"session": "20261111-172345-3fa91c", "title": "Flaky login test",
                 "connection": "claude", "connection_name": "Claude", "kind": "claude",
                 "folder": "/home/someone/src/shop", "source": {"label": "Chat", "detail": ""},
                 "n": 1, "started": 1794411900.0, "ended": 1794411942.0,
                 "state": "completed", "mode": "read", "model": "sonnet", "effort": "",
                 "cost_usd": 0.0412, "steps": 6, "error": ""}, **extra)


def _session(session_id="20261111-172345-3fa91c", title="Flaky login test", **extra):
    return dict({"id": session_id, "title": title, "connection": "claude",
                 "connection_name": "Claude", "kind": "claude",
                 "folder": "/home/someone/src/shop", "mode": "read", "model": "",
                 "effort": "", "restrictions": {}, "source": {"label": "Chat", "detail": ""},
                 "created": 1794411000.0, "updated": 1794411900.0, "state": "completed",
                 "running": False, "cost_usd": 0.04,
                 "turns": [{"n": 1, "state": "completed"}]}, **extra)


def _skill(name="odoo-review", description="Review Odoo modules.", **extra):
    """A skill as the library lists it."""
    return dict({"name": name, "description": description,
                 "files": ["SKILL.md"], "bytes": 2048, "scripts": False, "problem": "",
                 "source": {"kind": "git", "url": "https://example.invalid/owner/skills",
                            "ref": "main", "commit": "3f2a91c0" * 5,
                            "path": "skills/%s" % name, "added": "2026-11-11T18:00:00+0200"}},
                **extra)


def _candidate(pick, name, description="", **extra):
    """A skill as a look found it."""
    return dict({"pick": pick, "name": name, "description": description,
                 "files": ["SKILL.md"], "bytes": 2048, "scripts": False, "installed": False,
                 "staged": "0", "problems": []}, **extra)


class FakeCore:
    """Answers ``agents ...`` from canned payloads and writes every call down."""

    offload = False

    def __init__(self, connections=None, turns=None, kinds=None, sessions=None, events=None,
                 library=None, own=None, **answers):
        self.connections = list(connections if connections is not None else [_connection()])
        self.turns = list(turns or [])
        self.kinds = KINDS if kinds is None else kinds
        #: The skills library, what an agent has by itself, and what the next
        #: look will find.
        self.library = [dict(one) for one in library or []]
        self.own = dict(own or {})
        self.looked = {}
        #: Conversations as the core lists them, and what was said in each.
        self.sessions = [dict(one) for one in sessions or []]
        self.events = {key: list(value) for key, value in (events or {}).items()}
        self.answers = answers
        self.calls = []

    def _session(self, session_id):
        return next((one for one in self.sessions if one["id"] == session_id), None)

    def integrations(self, *args, document=None, timeout=120, cancel=None):
        """Jira's side of the page: a project with no connections."""
        return {"ok": True, "connections": [], "activity": []}

    def agents(self, *args, document=None, timeout=120, cancel=None):
        self.calls.append((args, document))
        command = args[0]
        if command in self.answers:
            answer = self.answers[command]
            return answer(args, document) if callable(answer) else answer
        if command == "list":
            return {"ok": True, "connections": [dict(one) for one in self.connections]}
        if command == "kinds":
            return {"ok": True, "kinds": [dict(one) for one in self.kinds]}
        if command == "models":
            return {"ok": True, "models": [
                {"value": "opus", "label": "Opus from CLI", "description": "Complex tasks"},
                {"value": "sonnet", "label": "Sonnet from CLI", "description": "Everyday tasks"}]}
        if command == "save":
            saved = dict(document["connection"])
            saved.setdefault("id", saved["name"].lower().replace(" ", "-"))
            return {"ok": True, "connection": saved}
        if command == "delete":
            return {"ok": True, "id": args[1]}
        if command == "test":
            answer = {"ok": True, "connection": args[1] if len(args) > 1 else "",
                      "result": {"account": "someone@example.invalid", "plan": "pro",
                                 "summary": "Signed in as someone@example.invalid (pro)"}}
            if len(args) > 1:
                answer["last_check"] = {"at": "2026-11-11T17:23:28+0200", "ok": True,
                                        "kind": "", "account": "someone@example.invalid",
                                        "error": ""}
            return answer
        if command == "sign-in":
            return {"ok": True, "argv": ["/bin/claude", "auth", "login", "--claudeai"],
                    "summary": "Sign in through the browser",
                    "detail": "Claude Code opens your browser and keeps the credentials itself."}
        if command == "activity":
            return {"ok": True, "activity": [dict(one) for one in self.turns]}
        if command == "sessions":
            # As the core matches a source's ref: exactly, or every one when none.
            ref = (document or {}).get("ref")
            found = [dict(one) for one in self.sessions
                     if not ref or (one.get("source") or {}).get("ref") == ref]
            return {"ok": True, "sessions": found, "total": len(found), "more": False}
        if command == "session":
            session = self._session(args[1])
            if session is None:
                return {"ok": False, "kind": "not_found",
                        "error": "There is no session %s." % args[1]}
            events = self.events.get(args[1], [])
            limit = (document or {}).get("limit") or 0
            return {"ok": True, "session": dict(session),
                    "events": [dict(one) for one in (events[-limit:] if limit else events)]}
        if command == "session-new":
            connection = next((one for one in self.connections
                               if one["id"] == document.get("connection")), None)
            if connection is None:
                return {"ok": False, "kind": "no_connection",
                        "error": "No agent connection %r." % document.get("connection")}
            session = _session("20261111-18%04d-abcdef" % len(self.sessions),
                               title=document.get("title") or "",
                               connection=connection["id"], connection_name=connection["name"],
                               folder=document.get("folder"), mode=document.get("mode"),
                               model=document.get("model") or "",
                               restrictions=dict(document.get("restrictions") or {}),
                               skills=list(document.get("skills") or []), turns=[],
                               plan=bool(document.get("plan")), phase="", record=False,
                               source=dict(document.get("source") or {"label": "Chat",
                                                                      "detail": ""}))
            self.sessions.insert(0, session)
            return {"ok": True, "session": dict(session)}
        if command == "session-set":
            session = self._session(args[1])
            session.update(document)
            return {"ok": True, "session": dict(session)}
        if command == "session-delete":
            self.sessions = [one for one in self.sessions if one["id"] != args[1]]
            self.events.pop(args[1], None)
            return {"ok": True, "id": args[1]}
        if command == "skills":
            return {"ok": True, "path": "/data/agent-skills",
                    "skills": [dict(one) for one in self.library], "own": dict(self.own)}
        if command == "skill-look":
            self.looked = {
                "token": "0123456789abcdef",
                "source": {"kind": "git", "url": "https://example.invalid/owner/skills",
                           "ref": "main", "commit": "3f2a91c0" * 5, "path": ""},
                "candidates": [
                    _candidate("skills/odoo-review", "odoo-review", "Review Odoo modules.",
                               files=["SKILL.md", "scripts/check.py"], scripts=True),
                    _candidate("skills/test-writer", "test-writer", "Write edge-case tests.")]}
            return dict(self.looked, ok=True)
        if command == "skill-add":
            added = []
            for one in self.looked.get("candidates") or []:
                if one["pick"] in document.get("picks", []):
                    skill = _skill(one["name"], one["description"], files=one["files"],
                                   scripts=one["scripts"])
                    self.library = [old for old in self.library if old["name"] != one["name"]]
                    self.library.append(skill)
                    added.append(skill)
            self.library.sort(key=lambda one: one["name"])
            return {"ok": True, "skills": added}
        if command == "skill-update":
            self.looked = {"token": "fedcba9876543210",
                           "candidates": [_candidate("", args[1], "Updated.")]}
            return {"ok": True, "token": "fedcba9876543210", "name": args[1],
                    "source": {"kind": "git", "url": "https://example.invalid/owner/skills",
                               "ref": "main", "commit": "9a8b7c6d" * 5, "path": ""},
                    "candidates": self.looked["candidates"], "was": "3f2a91c0" * 5,
                    "now": "9a8b7c6d" * 5,
                    "changes": {"added": ["notes.md"], "removed": [], "changed": ["SKILL.md"]}}
        if command == "skill-remove":
            self.library = [one for one in self.library if one["name"] != args[1]]
            return {"ok": True, "name": args[1]}
        raise AssertionError("unexpected agents command %r" % (args,))

    def asked(self):
        return [args[0] for args, _document in self.calls]


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
        page.open_provider("agents")
        if show:
            page.show()
            qapp.processEvents()
        pages.append(page)
        return page

    yield build
    for page in pages:
        dispose(page)


def _texts(table, row):
    return [table.item(row, column).text() for column in range(table.columnCount())]


# -- what is asked ----------------------------------------------------------------
def test_showing_the_page_reads_two_lists_and_starts_no_program(make):
    """The connections, and the conversations of the tab in front: both files -
    and where the agent programs are, for their effort levels: a look along
    PATH."""
    core = FakeCore()
    page = make(core)
    assert page.stack.currentWidget() is page.agents
    assert page.agents.tabs.currentWidget() is page.agents.chat
    assert core.asked() == ["list", "sessions", "kinds"]


def test_coming_back_asks_nothing_and_the_activity_is_read_when_its_tab_is_opened(make, qapp):
    core = FakeCore(turns=[_turn()])
    page = make(core)
    page.hide()
    page.show()
    qapp.processEvents()
    assert core.asked() == ["list", "sessions", "kinds"]
    page.agents.tabs.setCurrentWidget(page.agents.activity)
    assert core.asked() == ["list", "sessions", "kinds", "activity"]
    assert page.agents.activity.table.rowCount() == 1


def test_a_page_never_shown_asks_nothing(make):
    core = FakeCore()
    make(core, show=False)
    assert core.calls == []


def test_another_project_reads_again_and_drops_what_the_old_one_showed(make):
    page = make(FakeCore(turns=[_turn()]))
    page.agents.tabs.setCurrentWidget(page.agents.activity)
    assert page.agents.activity.table.rowCount() == 1
    other = FakeCore(connections=[_connection(), _connection("second", "Second")])
    page.set_core(other)
    assert other.asked() == ["list", "activity"]
    assert page.agents.activity.table.rowCount() == 0
    assert page.agents.connections_tab.table.rowCount() == 2


def test_a_core_that_cannot_answer_is_said_on_the_tab_not_raised(make):
    page = make(FakeCore(list={"ok": False, "kind": "error", "error": "cannot read agents.json"}))
    tab = page.agents.connections_tab
    assert tab.message.text() == "cannot read agents.json"
    assert tab.table.rowCount() == 0


def test_a_core_from_before_agents_is_an_answer_too(make, qapp, settings, dispose):
    class Old:
        offload = False

        def integrations(self, *args, **kwargs):
            return {"ok": True, "connections": []}

    page = IntegrationsPage(settings)
    try:
        page.set_core(Old())
        page.open_provider("agents")
        page.show()
        qapp.processEvents()
        assert "agents" in page.agents.connections_tab.message.text()
    finally:
        dispose(page)


# -- where it is kept ---------------------------------------------------------------
def test_connections_are_one_file_for_every_project_and_conversations_the_project_s(
        settings, tmp_path, monkeypatch):
    from cms_gui import core as core_mod, projectsfile
    home = tmp_path / "QAVector"
    monkeypatch.setattr(core_mod, "home_folder", lambda: str(home))
    assert settings.agents_path == str(home / "agents.json")
    assert settings.agent_sessions_path == str(home / "agent-sessions")
    settings.use_project(projectsfile.Project("shop", "Shop", str(home / "projects" / "shop")))
    assert settings.agents_path == str(home / "agents.json")
    assert settings.agent_sessions_path == str(home / "projects" / "shop" / "agent-sessions")


# -- the catalog ------------------------------------------------------------------
def test_the_catalog_lists_agents_after_jira_with_its_connections_counted(make, qapp):
    page = make(FakeCore(connections=[_connection(), _connection("second", "Second")]))
    page.agents.crumb.click()
    qapp.processEvents()
    assert page.stack.currentWidget() is page.catalog
    assert [page.table.item(row, 0).text() for row in range(page.table.rowCount())] == [
        "Jira", "Agents"]
    assert page.table.item(1, 1).text() == "2"
    # Jira was never opened: its connections are counted now, not left as a dash.
    assert page.table.item(0, 1).text() == "0"
    page.table.selectRow(1)
    page.open_selected()
    assert page.stack.currentWidget() is page.agents


def test_the_page_comes_back_to_agents_when_that_is_where_it_was_left(make, settings, qapp,
                                                                     dispose):
    make(FakeCore())
    assert settings.integrations_state()["view"] == "agents"
    again = IntegrationsPage(settings)
    try:
        again.set_core(FakeCore())
        assert again.stack.currentWidget() is again.agents
    finally:
        dispose(again)


def test_agents_opens_in_a_window_of_its_own_and_comes_back(make, qapp):
    page = make(FakeCore())
    window = page.detach_provider("agents")
    try:
        qapp.processEvents()
        assert page.agents.window() is window
        assert page.stack.currentWidget() is page.hosts["agents"].placeholder
        assert "Agents" in window.windowTitle()
    finally:
        page.hosts["agents"].dock()
    assert page.stack.currentWidget() is page.agents


# -- connections ------------------------------------------------------------------
def test_a_connection_s_row_says_what_runs_and_how_its_last_check_went(make):
    page = make(FakeCore(connections=[
        _connection(),
        _connection("reviews", "Reviews", binary="/opt/claude/bin/claude", model="opus",
                    effort="high",
                    last_check={"at": "2026-11-10T09:00:00+0200", "ok": True, "kind": "",
                                "account": "someone@example.invalid"}),
        _connection("old", "Old", last_check={
            "at": "2026-11-09T09:00:00+0200", "ok": False, "kind": "signin",
            "error": "Not signed in. Run `claude auth login` once."}),
        _connection("gone", "Gone", last_check={
            "at": "2026-11-09T09:00:00+0200", "ok": False, "kind": "not_installed",
            "error": "Claude Code is not installed."})]))
    table = page.agents.connections_tab.table
    assert _texts(table, 0) == ["Claude", "Claude Code", "Found automatically", "CLI default",
                                "Not checked"]
    assert _texts(table, 1) == ["Reviews", "Claude Code", "/opt/claude/bin/claude",
                                "opus · high effort",
                                "Signed in as someone@example.invalid · 2026-11-10"]
    assert table.item(2, 4).text() == "Not signed in · 2026-11-09"
    assert table.item(2, 4).toolTip() == "Not signed in. Run `claude auth login` once."
    assert table.item(2, 4).foreground().color() == QColor(theme.BAD_INK)
    assert table.item(3, 4).text() == "Not installed · 2026-11-09"
    assert table.item(1, 0).toolTip() == "id: reviews"


def test_no_connection_says_how_to_add_one_and_offers_only_that(make, qapp):
    page = make(FakeCore(connections=[]))
    tab = page.agents.connections_tab
    page.agents.tabs.setCurrentWidget(tab)
    qapp.processEvents()
    assert tab.note.isVisible()
    assert tab.note.text() == "No agent connection yet - + Connection adds one."
    assert tab.add_button.isEnabled()
    assert not any(button.isEnabled() for button in (
        tab.edit_button, tab.test_button, tab.sign_in_button, tab.remove_button))


def test_a_search_that_hides_every_row_says_so(make, qapp):
    page = make(FakeCore())
    tab = page.agents.connections_tab
    page.agents.tabs.setCurrentWidget(tab)
    tab.search.box(0).setText("nothing like it")
    qapp.processEvents()
    assert tab.note.isVisible() and tab.note.text() == "No connection matches the search."
    tab.search.box(0).setText("")
    qapp.processEvents()
    assert not tab.note.isVisible()


@pytest.mark.parametrize("which", ["connections", "activity"])
def test_every_table_has_a_search_box_over_every_column(make, qapp, which):
    page = make(FakeCore(turns=[_turn()]))
    tab = page.agents.connections_tab if which == "connections" else page.agents.activity
    page.agents.tabs.setCurrentWidget(tab)
    qapp.processEvents()
    header = tab.table.horizontalHeader()
    for column in range(tab.table.columnCount()):
        box = tab.search.box(column)
        assert box is not None and box.isVisible()
        assert box.x() == header.sectionViewportPosition(column)
        assert box.width() == header.sectionSize(column) + 1


def test_test_asks_the_program_once_and_the_row_remembers(make):
    core = FakeCore()
    page = make(core)
    tab = page.agents.connections_tab
    tab.table.selectRow(0)
    tab.test_button.click()
    assert core.calls[-1] == (("test", "claude"), None)
    assert tab.message.text() == "Signed in as someone@example.invalid (pro)"
    assert tab.table.item(0, 4).text() == "Signed in as someone@example.invalid · 2026-11-11"
    assert tab.test_button.isEnabled()


def test_a_failed_check_says_why_and_marks_the_row(make):
    failed = {"ok": False, "kind": "signin", "connection": "claude",
              "error": "Not signed in. Run `claude auth login` once.",
              "last_check": {"at": "2026-11-11T10:00:00+0200", "ok": False, "kind": "signin",
                             "error": "Not signed in. Run `claude auth login` once."}}
    page = make(FakeCore(test=failed))
    tab = page.agents.connections_tab
    tab.table.selectRow(0)
    tab.test_button.click()
    assert tab.message.text() == "Not signed in. Run `claude auth login` once."
    assert tab.table.item(0, 4).text() == "Not signed in · 2026-11-11"


def test_sign_in_starts_the_program_the_core_names_and_says_what_next(make, monkeypatch):
    started = []
    monkeypatch.setattr(agents_module, "start_detached", started.append)
    core = FakeCore()
    page = make(core)
    tab = page.agents.connections_tab
    tab.table.selectRow(0)
    tab.sign_in_button.click()
    assert core.calls[-1] == (("sign-in", "claude"), None)
    assert started == [["/bin/claude", "auth", "login", "--claudeai"]]
    assert "Press Test when the browser says you are signed in." in tab.message.text()


def test_sign_in_to_a_program_that_is_not_there_starts_nothing(make, monkeypatch):
    started = []
    monkeypatch.setattr(agents_module, "start_detached", started.append)
    page = make(FakeCore(**{"sign-in": {"ok": False, "kind": "not_installed",
                                        "error": "Claude Code is not installed."}}))
    tab = page.agents.connections_tab
    tab.table.selectRow(0)
    tab.sign_in_button.click()
    assert started == [] and tab.message.text() == "Claude Code is not installed."


def test_removing_asks_first_and_says_what_is_left_alone(make, monkeypatch):
    asked = []
    monkeypatch.setattr(widgets, "confirm", lambda *args, **kw: asked.append(args) or True)
    core = FakeCore()
    page = make(core)
    tab = page.agents.connections_tab
    tab.table.selectRow(0)
    tab.remove()
    assert asked[0][2] == "Remove Claude?"
    assert "sign-in, are left alone" in asked[0][3]
    assert core.calls[-1][0] == ("delete", "claude")
    assert page.agents.connections == [] and tab.table.rowCount() == 0


def test_saying_no_removes_nothing(make, monkeypatch):
    monkeypatch.setattr(widgets, "confirm", lambda *args, **kw: False)
    core = FakeCore()
    page = make(core)
    tab = page.agents.connections_tab
    tab.table.selectRow(0)
    tab.remove()
    assert "delete" not in core.asked() and tab.table.rowCount() == 1


# -- the dialog -------------------------------------------------------------------
def _dialog(page, connection=None, taken=(), kinds=KINDS):
    # Over the tab, as the tab opens it.
    dialog = ConnectionDialog(page.agents.connections_tab, connection, taken=taken,
                              kinds=kinds)
    dialog.save_requested.connect(page.agents.save_connection)
    dialog.check_requested.connect(page.agents.check_connection)
    return dialog


def test_a_first_connection_needs_no_thought(make, dispose):
    """It is called what it is, and its program is found."""
    core = FakeCore(connections=[])
    page = make(core)
    dialog = _dialog(page)
    try:
        assert dialog.name.text() == "Claude Code"
        assert dialog.kind.currentData() == "claude" and dialog.kind.isEnabled()
        assert dialog.binary.text() == ""
        assert dialog.found.text() == "Blank uses /home/someone/.local/bin/claude, found on PATH."
        assert [dialog.effort.itemData(index) for index in range(dialog.effort.count())] == [
            "", "low", "medium", "high", "xhigh", "max"]
        dialog.save.click()
        assert core.calls[-1] == (("save",), {"connection": {
            "name": "Claude Code", "kind": "claude", "binary": "", "model": "", "effort": "",
            "qavector_access": {"services": False, "scenarios": False, "cycles": False, "approvals": False, "jira": False}}})
        assert dialog.result() == QDialog.Accepted
    finally:
        dispose(dialog)
    tab = page.agents.connections_tab
    assert tab.table.rowCount() == 1 and tab.selected()["name"] == "Claude Code"


def test_a_program_that_is_nowhere_is_said_before_anything_is_saved(make, dispose):
    page = make(FakeCore())
    dialog = _dialog(page, kinds=[dict(KINDS[0], found="", install_hint="Install it with npm.")])
    try:
        assert dialog.found.text() == ("Claude Code is not installed on this computer. Install "
                                       "it with npm. Or give the path of its program.")
        # Installed since, outside PATH: the dialog says where it was found.
        # The same dialog: one more would only add a window to the process.
        dialog.set_kinds([dict(KINDS[0], found="/opt/app/claude", found_where="the app")])
        assert dialog.found.text() == "Blank uses /opt/app/claude, found in the app."
        assert dialog.binary.placeholderText() == "claude, found automatically"
    finally:
        dispose(dialog)


def test_the_dialog_opens_at_once_and_fills_in_when_the_core_answers(make, dispose):
    page = make(FakeCore())
    dialog = _dialog(page, kinds=[])
    try:
        assert dialog.kind.currentText() == "Claude Code"
        assert dialog.found.text() == "Blank finds claude on PATH each time it is used."
        dialog.model.set_value("opus")
        dialog.set_kinds(KINDS)
        assert "/home/someone/.local/bin/claude" in dialog.found.text()
        assert dialog.model.value() == "opus"
    finally:
        dispose(dialog)


def test_adding_through_the_tab_asks_where_the_programs_are_once(make, monkeypatch):
    seen = []

    def exec_(dialog):
        seen.append((dialog.found.text(), dialog.name.text()))
        return QDialog.Rejected

    monkeypatch.setattr(ConnectionDialog, "exec", exec_)
    core = FakeCore(connections=[])
    page = make(core)
    before = core.asked().count("kinds")            # the chat's, for effort levels
    page.agents.connections_tab.add_button.click()
    assert core.asked().count("kinds") == before + 1 and "test" not in core.asked()
    assert seen == [("Blank uses /home/someone/.local/bin/claude, found on PATH.", "Claude Code")]


def test_a_name_already_taken_or_missing_is_said_without_asking_the_core(make, dispose):
    core = FakeCore()
    page = make(core)
    dialog = _dialog(page, taken=["Claude"])
    try:
        dialog.name.setText("claude")
        dialog.save.click()
        assert dialog.problem.text() == "A connection called 'claude' already exists."
        dialog.name.setText("  ")
        dialog.save.click()
        assert dialog.problem.text() == "Name is required."
        assert "save" not in core.asked()
    finally:
        dispose(dialog)


def test_what_the_core_refuses_is_shown_and_the_dialog_stays(make, dispose):
    core = FakeCore(save={"ok": False, "kind": "invalid",
                          "problems": ["Effort must be one of low, medium."]})
    page = make(core)
    dialog = _dialog(page)
    try:
        dialog.name.setText("Another")
        dialog.save.click()
        assert dialog.problem.isVisibleTo(dialog) or dialog.problem.text()
        assert dialog.problem.text() == "Effort must be one of low, medium."
        assert dialog.result() != QDialog.Accepted and dialog.save.text() == "Add"
    finally:
        dispose(dialog)


def test_editing_keeps_the_id_and_the_agent_and_sends_what_changed(make, dispose):
    core = FakeCore(connections=[_connection(model="sonnet", last_check={"ok": True})])
    page = make(core)
    dialog = _dialog(page, page.agents.connections[0])
    try:
        assert dialog.windowTitle() == "Edit connection" and dialog.save.text() == "Save"
        assert not dialog.kind.isEnabled()
        assert dialog.name.text() == "Claude" and dialog.model.value() == "sonnet"
        dialog.binary.setText("/opt/claude/bin/claude")
        dialog.effort.setCurrentIndex(dialog.effort.findData("high"))
        dialog.save.click()
        assert core.calls[-1] == (("save",), {"connection": {
            "id": "claude", "name": "Claude", "kind": "claude",
            "binary": "/opt/claude/bin/claude", "model": "sonnet", "effort": "high",
            "qavector_access": {"services": False, "scenarios": False, "cycles": False, "approvals": False, "jira": False}}})
    finally:
        dispose(dialog)
    assert page.agents.connections_tab.table.item(0, 2).text() == "/opt/claude/bin/claude"


def test_check_asks_about_the_form_as_it_stands_and_saves_nothing(make, dispose):
    core = FakeCore(connections=[])
    page = make(core)
    dialog = _dialog(page)
    try:
        dialog.binary.setText("/opt/claude/bin/claude")
        dialog.check.click()
        args, document = core.calls[-1]
        assert args == ("test",)
        assert document["connection"]["binary"] == "/opt/claude/bin/claude"
        assert dialog.state.text() == "Signed in as someone@example.invalid (pro)"
        assert dialog.state.property("role") == "hint"
        assert "save" not in core.asked() and dialog.check.isEnabled()
    finally:
        dispose(dialog)


def test_a_check_that_fails_is_said_as_a_problem(make, dispose):
    page = make(FakeCore(test={"ok": False, "kind": "not_installed",
                               "error": "Claude Code is not installed."}))
    dialog = _dialog(page)
    try:
        dialog.check.click()
        assert dialog.state.text() == "Claude Code is not installed."
        assert dialog.state.property("role") == "error"
    finally:
        dispose(dialog)


def test_the_dialog_s_fields_are_one_width(make, qapp, dispose):
    page = make(FakeCore())
    dialog = _dialog(page)
    try:
        dialog.show()
        qapp.processEvents()
        assert len({widget.width() for widget in (dialog.name, dialog.kind, dialog.model,
                                                  dialog.effort)}) == 1
        # The program's box and its Browse button together are that width too.
        assert dialog.browse.geometry().right() + dialog.browse.parentWidget().x() == \
            dialog.name.geometry().right()
        assert dialog.binary.x() + dialog.binary.parentWidget().x() == dialog.name.x()
        # A quiet link beside its field, as beside every Project folder: lower
        # than the field, and on its centre line.
        assert dialog.browse.property("variant") == "ghost"
        assert abs(dialog.binary.geometry().center().y()
                   - dialog.browse.geometry().center().y()) <= 1
    finally:
        dispose(dialog)


# -- activity ---------------------------------------------------------------------
def test_a_turn_s_row_says_when_who_where_and_how_it_went(make):
    page = make(FakeCore(turns=[
        _turn(),
        _turn(n=2, state="failed", error="Claude Code ended\nwithout an answer (exit 3).",
              source={"label": "Cycle nightly · step review", "detail": "Run 20261111-1"},
              mode="edit", model="", cost_usd=None, ended=1794412090.0,
              skills=["odoo-review", "test-writer"]),
        _turn(n=3, state="running", ended=None, cost_usd=None)]))
    view = page.agents
    view.tabs.setCurrentWidget(view.activity)
    table = view.activity.table
    assert _texts(table, 0) == [agents_module.when_text(1794411900.0), "Claude", "Chat",
                                "/home/someone/src/shop", "read", "sonnet", "—", "42 s",
                                "—", "$0.04", "Done"]
    assert table.item(0, 0).toolTip() == ("Flaky login test\nDouble-click to read the "
                                          "conversation.")
    assert _texts(table, 1)[2:] == ["Cycle nightly · step review", "/home/someone/src/shop",
                                    "edit", "CLI default", "2", "3 min 10 s", "—", "—",
                                    "Failed - Claude Code ended without an answer (exit 3)."]
    assert table.item(1, 2).toolTip() == "Run 20261111-1"
    # How many skills it was handed; which ones is a pointer away.
    assert table.item(1, 6).toolTip() == "odoo-review, test-writer"
    assert table.item(1, 10).foreground().color() == QColor(theme.BAD_INK)
    assert _texts(table, 2)[7:] == ["—", "—", "—", "Running"]


def test_nothing_asked_yet_says_so(make, qapp):
    page = make(FakeCore())
    view = page.agents
    view.tabs.setCurrentWidget(view.activity)
    qapp.processEvents()
    assert view.activity.note.isVisible()
    assert view.activity.note.text().startswith("No agent has been asked anything")


def test_opening_the_activity_again_reads_it_again(make, qapp):
    """A cycle may have taken a turn meanwhile: the record is cheap to read."""
    core = FakeCore(turns=[_turn()])
    page = make(core)
    view = page.agents
    view.tabs.setCurrentWidget(view.activity)
    view.tabs.setCurrentWidget(view.connections_tab)
    core.turns.append(_turn(n=2))
    view.tabs.setCurrentWidget(view.activity)
    assert core.asked().count("activity") == 2
    assert view.activity.table.rowCount() == 2


def test_an_activity_that_cannot_be_read_is_said(make):
    page = make(FakeCore(activity={"ok": False, "kind": "error", "error": "cannot read it"}))
    view = page.agents
    view.tabs.setCurrentWidget(view.activity)
    assert view.activity.message.text() == "cannot read it"


@pytest.mark.parametrize("started, ended, said", [
    (0, 0.4, "0 s"), (0, 59.4, "59 s"), (0, 60, "1 min 00 s"), (0, 3725, "1 h 02 min"),
    (10, None, ""), ("x", 3, "")])
def test_how_long_a_turn_took_is_said_the_way_a_person_says_it(started, ended, said):
    assert agents_module.took_text(started, ended) == said


def test_a_folder_under_home_is_said_from_home_and_kept_whole_in_the_tooltip(make, monkeypatch):
    """The end of a path tells two projects apart, and a column cuts a line there."""
    monkeypatch.setenv("HOME", "/home/someone")
    assert agents_module.folder_text("/home/someone/src/shop") == "~/src/shop"
    assert agents_module.folder_text("/home/someone") == "~"
    assert agents_module.folder_text("/home/someone-else/src") == "/home/someone-else/src"
    assert agents_module.folder_text("/srv/shop") == "/srv/shop"
    page = make(FakeCore(turns=[_turn()]))
    page.agents.tabs.setCurrentWidget(page.agents.activity)
    cell = page.agents.activity.table.item(0, 3)
    assert (cell.text(), cell.toolTip()) == ("~/src/shop", "/home/someone/src/shop")


@pytest.mark.parametrize("value, said", [(0.0412, "$0.04"), (0.004, "< $0.01"), (0, "$0.00"),
                                         (12.5, "$12.50"), (None, ""), (True, "")])
def test_what_a_turn_cost_is_said_in_cents(value, said):
    assert agents_module.cost_text(value) == said


# -- leaving ----------------------------------------------------------------------
def test_the_page_has_nothing_unsaved_and_is_busy_only_with_a_dialog_or_a_save(make, dispose):
    page = make(FakeCore())
    assert page.has_unsaved() is False and page.busy_reason() == ""
    dialog = _dialog(page)
    try:
        dialog.show()
        assert page.busy_reason() == "Finish or cancel the open Agents dialog first."
        assert page.busy_reason(include_dialogs=False) == ""
    finally:
        dispose(dialog)


def test_after_shutdown_nothing_more_is_asked(make, qapp):
    core = FakeCore()
    page = make(core)
    before = core.asked()
    page.shutdown()
    page.agents.tabs.setCurrentWidget(page.agents.activity)
    page.agents.list_connections()
    page.agents.chat.load_sessions()
    page.hide()
    page.show()
    qapp.processEvents()
    assert core.asked() == before


def test_a_live_theme_change_repaints_what_went_wrong(make, qapp):
    page = make(FakeCore(connections=[_connection(last_check={
        "at": "2026-11-09T09:00:00+0200", "ok": False, "kind": "signin", "error": "x"})]))
    table = page.agents.connections_tab.table
    light = QColor(theme.BAD_INK)
    assert table.item(0, 4).foreground().color() == light
    try:
        theme.set_dark_mode(True)
        qapp.setStyleSheet(theme.stylesheet())
        qapp.processEvents()
        # Red as text is a lighter red on the dark ground: the one it was
        # painted with would be hard to read there.
        assert QColor(theme.BAD_INK) != light
        assert table.item(0, 4).foreground().color() == QColor(theme.BAD_INK)
    finally:
        theme.set_dark_mode(False)
        qapp.setStyleSheet(theme.stylesheet())


# -- search (Ctrl+K) ------------------------------------------------------------------
def test_search_offers_what_is_loaded_and_never_asks_anything_more(make):
    session = _session()
    core = FakeCore(connections=[_connection(model="opus")], sessions=[session],
                    library=[_skill()])
    page = make(core)
    view = page.agents
    view.tabs.setCurrentWidget(view.skills_tab)
    asked = list(core.calls)
    hits = page.search_rows()
    assert core.calls == asked, "search reads only what the page has loaded"
    titles = {hit.title: hit for hit in hits if hit.target[0] == "agents"}
    assert titles["Claude"].detail == "Agent connection · Found automatically"
    assert titles["Flaky login test"].detail == ("Conversation · Claude · "
                                                 "/home/someone/src/shop")
    assert titles["odoo-review"].detail == "Skill · Review Odoo modules."
    assert view.search_source() is None


def test_a_found_conversation_skill_or_connection_is_shown_where_it_lives(make):
    session = _session()
    core = FakeCore(sessions=[session], library=[_skill()])
    page = make(core)
    view = page.agents
    view.tabs.setCurrentWidget(view.skills_tab)
    assert page.show_found(("agents", ("skill", "odoo-review")))
    assert view.tabs.currentWidget() is view.skills_tab
    assert view.skills_tab.selected()["name"] == "odoo-review"
    assert page.show_found(("agents", ("connection", "claude")))
    assert view.connections_tab.selected()["id"] == "claude"
    assert page.show_found(("agents", ("session", session["id"])))
    assert view.tabs.currentWidget() is view.chat
    assert view.chat.current["id"] == session["id"]
    assert not page.show_found(("agents", ("session", "20200101-000000-aaaaaa")))
    assert not page.show_found(("agents", ("skill", "gone")))



def test_qavector_access_is_off_until_a_switch_is_turned_on(qapp):
    """QCR ADR-003 D6: what an agent may do in QAVector, per connection, nothing by default."""
    from cms_gui.pages.agents import ConnectionDialog
    dialog = ConnectionDialog(None, {"id": "claude", "name": "Claude", "kind": "claude",
                                     "qavector_access": {"services": True}})
    try:
        assert dialog.entry()["qavector_access"] == {"services": True, "scenarios": False, "cycles": False,
                                                      "approvals": False, "jira": False}
        dialog.access["approvals"].setChecked(True)
        assert dialog.entry()["qavector_access"]["approvals"] is True
        assert ConnectionDialog(None).entry()["qavector_access"] == dict.fromkeys(
            ("services", "scenarios", "cycles", "approvals", "jira"), False)
    finally:
        dialog.close()
