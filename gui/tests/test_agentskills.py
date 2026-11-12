"""Integrations -> Agents -> Skills: the library, adding to it, choosing from it.

The promise the page makes is the one its lede says: nothing is added unseen.
So the tests are about the order of things - looking shows and adds nothing,
adding takes what was looked at, an update says what changed before any of it
is taken - and about the network: nothing is fetched by showing a tab or
opening a list, only by pressing Look or Update.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QDialog

from cms_gui import widgets
from cms_gui.pages import agentskills
from cms_gui.pages.agentskills import AddSkillDialog
from cms_gui.pages.integrations import IntegrationsPage
from cms_gui.settings import Settings
from test_agentchat import FakeTurn, _events
from test_agents_page import FakeCore, _candidate, _session, _skill


@pytest.fixture
def settings(tmp_path):
    made = Settings()
    made._qs = QSettings(str(tmp_path / "gui.ini"), QSettings.IniFormat)
    return made


@pytest.fixture
def make(qapp, settings, dispose, tmp_path):
    pages = []
    FakeTurn.made = []

    def build(core=None, tab="skills"):
        settings.save_integrations_state({"agents": {"folders": [str(tmp_path)]}})
        page = IntegrationsPage(settings)
        page.agents.chat.turn_factory = FakeTurn
        page.resize(1200, 800)
        page.set_core(core or FakeCore())
        page.open_provider("agents")
        page.show()
        qapp.processEvents()
        if tab == "skills":
            page.agents.tabs.setCurrentWidget(page.agents.skills_tab)
            qapp.processEvents()
        pages.append(page)
        return page

    yield build
    for page in pages:
        dispose(page)


def _texts(table, row):
    return [table.item(row, column).text() for column in range(table.columnCount())]


def _dialog(page):
    tab = page.agents.skills_tab
    dialog = AddSkillDialog(tab)
    dialog.look_requested.connect(page.agents.look_at_skill)
    dialog.add_requested.connect(page.agents.add_skills)
    return dialog


# -- saying things ------------------------------------------------------------------
def test_where_a_skill_came_from_is_said_short_enough_for_a_column(monkeypatch):
    monkeypatch.setenv("HOME", "/home/someone")
    git = {"kind": "git", "url": "https://example.invalid/owner/skills.git", "ref": "main",
           "commit": "3f2a91c0" * 5, "path": "skills/odoo-review"}
    assert agentskills.source_text(git) == "example.invalid/owner/skills · skills/odoo-review"
    assert agentskills.source_text(dict(git, path="")) == "example.invalid/owner/skills"
    assert agentskills.version_text(git) == "main @ 3f2a91c"
    assert agentskills.version_text(dict(git, ref="")) == "3f2a91c"
    folder = {"kind": "folder", "folder": "/home/someone/skills/mine"}
    assert agentskills.source_text(folder) == "~/skills/mine"
    assert agentskills.version_text(folder) == "A folder's copy"
    assert agentskills.source_text({}) == "" and agentskills.version_text({}) == ""


def test_how_much_a_skill_holds_says_when_some_of_it_could_be_run():
    assert agentskills.files_text({"files": ["SKILL.md"], "bytes": 512}) == "1 file · 512 B"
    assert agentskills.files_text({"files": ["a", "b"], "bytes": 4096, "scripts": True}) == (
        "2 files · 4 KB · has scripts")
    assert agentskills.size_text(3 * 1024 * 1024) == "3.0 MB"


def test_what_changed_is_listed_a_file_a_line():
    assert agentskills.changes_text({"changes": {
        "added": ["notes.md"], "removed": ["old.sh"], "changed": ["SKILL.md"]}}) == (
        "Changed   SKILL.md\nAdded     notes.md\nRemoved   old.sh")
    assert agentskills.changes_text({"changes": {"added": [], "removed": [], "changed": []}}) == ""


# -- the library ----------------------------------------------------------------------
def test_opening_the_tab_reads_the_library_and_fetches_nothing(make):
    core = FakeCore(library=[_skill(), _skill("test-writer", "Write edge-case tests.")])
    page = make(core)
    tab = page.agents.skills_tab
    assert core.asked().count("skills") == 1
    assert "skill-look" not in core.asked() and "skill-update" not in core.asked()
    assert _texts(tab.table, 0) == ["odoo-review", "Review Odoo modules.",
                                    "example.invalid/owner/skills · skills/odoo-review",
                                    "main @ 3f2a91c", "2026-11-11"]
    assert tab.table.item(0, 0).toolTip() == "1 file · 2 KB"
    assert tab.table.item(0, 3).toolTip() == "3f2a91c0" * 5


def test_a_page_that_never_showed_the_tab_never_read_the_library(make):
    core = FakeCore(library=[_skill()])
    make(core, tab="chat")
    assert "skills" not in core.asked()


def test_no_skills_yet_says_how_to_add_one(make, qapp):
    tab = make(FakeCore()).agents.skills_tab
    assert tab.note.isVisible()
    assert tab.note.text() == "No skills yet - + Skill adds one from a link or a folder."
    assert tab.add_button.isEnabled()
    assert not tab.update_button.isEnabled() and not tab.remove_button.isEnabled()
    assert not tab.own_line.isVisible()


def test_what_the_agent_has_by_itself_is_said_under_the_list(make):
    tab = make(FakeCore(own={"claude": ["pdf", "docx"]})).agents.skills_tab
    assert tab.own_line.isVisible()
    assert tab.own_line.text().endswith("they are not kept here: docx, pdf.")


def test_a_skill_somebody_broke_says_what_is_wrong_in_place_of_what_it_is_for(make, qapp):
    from PySide6.QtGui import QColor
    from cms_gui import theme
    page = make(FakeCore(library=[_skill(problem="There is no SKILL.md in it.",
                                         description="")]))
    tab = page.agents.skills_tab
    assert tab.table.item(0, 1).text() == "There is no SKILL.md in it."
    assert tab.table.item(0, 1).foreground().color() == QColor(theme.BAD_INK)
    try:
        theme.set_dark_mode(True)
        qapp.setStyleSheet(theme.stylesheet())
        qapp.processEvents()
        # A lighter red on the dark ground, repainted where it stands.
        assert tab.table.item(0, 1).foreground().color() == QColor(theme.BAD_INK)
    finally:
        theme.set_dark_mode(False)
        qapp.setStyleSheet(theme.stylesheet())


def test_a_search_that_hides_every_skill_says_so(make, qapp):
    tab = make(FakeCore(library=[_skill()])).agents.skills_tab
    tab.search.box(0).setText("nothing like it")
    qapp.processEvents()
    assert tab.note.isVisible() and tab.note.text() == "No skill matches the search."


def test_the_library_has_a_search_box_over_every_column(make, qapp):
    tab = make(FakeCore(library=[_skill()])).agents.skills_tab
    header = tab.table.horizontalHeader()
    for column in range(tab.table.columnCount()):
        box = tab.search.box(column)
        assert box is not None and box.isVisible()
        assert box.x() == header.sectionViewportPosition(column)
        assert box.width() == header.sectionSize(column) + 1


def test_a_skill_with_no_known_source_can_be_removed_but_not_looked_at_again(make):
    tab = make(FakeCore(library=[_skill(source={})])).agents.skills_tab
    tab.table.selectRow(0)
    assert tab.remove_button.isEnabled() and not tab.update_button.isEnabled()


# -- adding -----------------------------------------------------------------------------
def test_looking_shows_what_is_there_and_adds_nothing(make, dispose):
    core = FakeCore()
    page = make(core)
    dialog = _dialog(page)
    try:
        assert not dialog.look.isEnabled() and not dialog.add.isEnabled()
        dialog.where.setText("https://example.invalid/owner/skills")
        dialog.look.click()
        assert core.calls[-1] == (("skill-look",),
                                  {"url": "https://example.invalid/owner/skills"})
        assert dialog.source.text() == "main @ 3f2a91c · https://example.invalid/owner/skills"
        assert _texts(dialog.table, 0) == ["odoo-review", "Review Odoo modules.",
                                           "2 files · 2 KB · has scripts", "—"]
        # Every file of the skill picked, before it is anybody's.
        assert dialog.files.toPlainText() == "SKILL.md\nscripts/check.py"
        dialog.table.selectRow(1)
        assert dialog.files.toPlainText() == "SKILL.md"
        # Among several, a person says which: none is ticked for them.
        assert dialog.picks() == [] and not dialog.add.isEnabled()
        assert "skill-add" not in core.asked() and core.library == []
    finally:
        dispose(dialog)


def test_adding_takes_what_was_looked_at_and_the_library_has_it(make, dispose):
    core = FakeCore()
    page = make(core)
    dialog = _dialog(page)
    try:
        dialog.where.setText("https://example.invalid/owner/skills")
        dialog.look.click()
        dialog.table.item(0, 0).setCheckState(Qt.Checked)
        assert dialog.add.isEnabled()
        dialog.add.click()
        assert core.calls[-2] == (("skill-add",), {"token": "0123456789abcdef",
                                                   "picks": ["skills/odoo-review"],
                                                   "replace": False})
        assert dialog.result() == QDialog.Accepted
    finally:
        dispose(dialog)
    tab = page.agents.skills_tab
    assert [tab.table.item(row, 0).text() for row in range(tab.table.rowCount())] == [
        "odoo-review"]


def test_the_one_skill_found_is_ticked_for_you(make, dispose):
    one = {"ok": True, "token": "0123456789abcdef",
           "source": {"kind": "folder", "folder": "/home/someone/skills/mine"},
           "candidates": [_candidate("", "mine", "My own.")]}
    core = FakeCore(**{"skill-look": one})
    dialog = _dialog(make(core))
    try:
        dialog.kind.set_current(agentskills.FOLDER)
        assert dialog.browse.isVisibleTo(dialog)
        dialog.where.setText("/home/someone/skills/mine")
        dialog.look.click()
        assert core.calls[-1] == (("skill-look",), {"folder": "/home/someone/skills/mine"})
        assert dialog.picks() == [""] and dialog.add.isEnabled()
        assert dialog.source.text() == "A folder's copy · /home/someone/skills/mine"
    finally:
        dispose(dialog)


def test_a_link_has_no_browse_and_says_what_is_and_is_not_sent(make, dispose):
    dialog = _dialog(make(FakeCore()))
    try:
        assert not dialog.browse.isVisibleTo(dialog)
        assert "nothing of yours is sent" in dialog.hint.text()
        dialog.kind.set_current(agentskills.FOLDER)
        assert "left as it is" in dialog.hint.text()
    finally:
        dispose(dialog)


def test_what_could_not_be_looked_at_says_why_and_offers_nothing_to_add(make, dispose):
    refused = {"ok": False, "kind": "unreachable",
               "error": "git could not fetch it: repository not found"}
    dialog = _dialog(make(FakeCore(**{"skill-look": refused})))
    try:
        dialog.where.setText("https://example.invalid/owner/nothing")
        dialog.look.click()
        assert dialog.problem.text() == "git could not fetch it: repository not found"
        assert dialog.table.rowCount() == 0 and not dialog.add.isEnabled()
        assert dialog.look.isEnabled() and dialog.look.text() == "Look"
        dialog.where.setText("https://example.invalid/owner/skills")
        assert dialog.problem.text() == ""               # typing again clears what was said
    finally:
        dispose(dialog)


def test_a_skill_with_something_wrong_cannot_be_ticked_and_says_what(make, dispose):
    looked = {"ok": True, "token": "0123456789abcdef",
              "source": {"kind": "git", "url": "https://example.invalid/o/r", "ref": "main",
                         "commit": "3f2a91c0" * 5, "path": ""},
              "candidates": [
                  _candidate("good", "good", "Fine."),
                  _candidate("bad", "bad", staged="",
                             problems=["host is a link to somewhere outside the skill."])]}
    dialog = _dialog(make(FakeCore(**{"skill-look": looked})))
    try:
        dialog.where.setText("https://example.invalid/o/r")
        dialog.look.click()
        bad = dialog.table.item(1, 0)
        assert not bad.flags() & Qt.ItemIsUserCheckable and not bad.flags() & Qt.ItemIsEnabled
        assert dialog.table.item(1, 3).text() == (
            "host is a link to somewhere outside the skill.")
        # The other is the one usable thing found, so it is ticked.
        assert dialog.picks() == ["good"]
    finally:
        dispose(dialog)


def test_one_the_library_already_has_is_replaced_only_when_ticked(make, dispose):
    looked = {"ok": True, "token": "0123456789abcdef",
              "source": {"kind": "folder", "folder": "/skills"},
              "candidates": [_candidate("", "odoo-review", "Newer.", installed=True)]}
    core = FakeCore(library=[_skill()], **{"skill-look": looked})
    core.looked = looked
    dialog = _dialog(make(core))
    try:
        dialog.where.setText("/skills")
        dialog.kind.set_current(agentskills.FOLDER)
        dialog.look.click()
        assert dialog.picks() == []                      # not ticked for you: it replaces
        assert dialog.table.item(0, 3).text() == "In the library: adding replaces it"
        dialog.table.item(0, 0).setCheckState(Qt.Checked)
        dialog.add.click()
        assert core.calls[-2][1]["replace"] is True
    finally:
        dispose(dialog)


def test_a_refused_add_is_said_and_the_dialog_stays(make, dispose):
    core = FakeCore(**{"skill-add": {"ok": False, "kind": "expired",
                                     "error": "What was looked at is gone. Look again."}})
    dialog = _dialog(make(core))
    try:
        dialog.where.setText("https://example.invalid/owner/skills")
        dialog.look.click()
        dialog.table.item(0, 0).setCheckState(Qt.Checked)
        dialog.add.click()
        assert dialog.problem.text() == "What was looked at is gone. Look again."
        assert dialog.result() != QDialog.Accepted and dialog.add.text() == "Add"
    finally:
        dispose(dialog)


def test_what_a_skill_holds_is_shown_whole(make, qapp, dispose):
    """Files, size and whether any could run: the part read before saying yes."""
    dialog = _dialog(make(FakeCore()))
    try:
        dialog.show()
        dialog.where.setText("https://example.invalid/owner/skills")
        dialog.look.click()
        qapp.processEvents()
        cell = dialog.table.item(0, 2)
        wanted = dialog.table.fontMetrics().horizontalAdvance(cell.text())
        assert dialog.table.columnWidth(2) > wanted
    finally:
        dispose(dialog)


def test_the_add_dialog_s_table_has_a_search_box_over_every_column(make, qapp, dispose):
    dialog = _dialog(make(FakeCore()))
    try:
        dialog.show()
        dialog.where.setText("https://example.invalid/owner/skills")
        dialog.look.click()
        qapp.processEvents()
        header = dialog.table.horizontalHeader()
        for column in range(dialog.table.columnCount()):
            box = dialog.search.box(column)
            assert box is not None and box.isVisible()
            assert box.x() == header.sectionViewportPosition(column)
            assert box.width() == header.sectionSize(column) + 1
    finally:
        dispose(dialog)


# -- updating and removing ------------------------------------------------------------
def test_an_update_says_what_changed_before_any_of_it_is_taken(make, monkeypatch):
    asked = []
    monkeypatch.setattr(widgets, "confirm_output",
                        lambda *args, **kw: asked.append((args, kw)) or True)
    core = FakeCore(library=[_skill()])
    tab = make(core).agents.skills_tab
    tab.table.selectRow(0)
    tab.update_button.click()
    assert ("skill-update", "odoo-review") in [args for args, _doc in core.calls]
    (args, kw), = asked
    assert args[2] == "Take what changed in odoo-review?"
    assert "main @ 3f2a91c -> main @ 9a8b7c6" in args[3]
    assert kw["text"] == "Changed   SKILL.md\nAdded     notes.md"
    add = next(document for args, document in core.calls if args[0] == "skill-add")
    assert add == {"token": "fedcba9876543210", "picks": [""], "replace": True}
    assert tab.message.text() == "odoo-review updated."


def test_declining_an_update_takes_none_of_it(make, monkeypatch):
    monkeypatch.setattr(widgets, "confirm_output", lambda *args, **kw: False)
    core = FakeCore(library=[_skill()])
    tab = make(core).agents.skills_tab
    tab.table.selectRow(0)
    tab.update_button.click()
    assert "skill-add" not in core.asked()
    assert tab.update_button.isEnabled()


def test_nothing_new_at_the_source_is_said_and_nothing_is_asked(make, monkeypatch):
    monkeypatch.setattr(widgets, "confirm_output",
                        lambda *args, **kw: pytest.fail("nothing changed: nothing to ask"))
    same = {"ok": True, "token": "fedcba9876543210", "name": "odoo-review",
            "source": {"kind": "git", "url": "https://example.invalid/o/r", "ref": "main",
                       "commit": "3f2a91c0" * 5, "path": ""},
            "candidates": [], "was": "3f2a91c0" * 5, "now": "3f2a91c0" * 5,
            "changes": {"added": [], "removed": [], "changed": []}}
    core = FakeCore(library=[_skill()], **{"skill-update": same})
    tab = make(core).agents.skills_tab
    tab.table.selectRow(0)
    tab.update_button.click()
    assert tab.message.text() == "odoo-review is as its source is now - main @ 3f2a91c."
    assert "skill-add" not in core.asked()


def test_a_source_that_cannot_be_reached_is_said_and_the_skill_stays(make):
    gone = {"ok": False, "kind": "unreachable", "error": "git could not fetch it: timed out"}
    core = FakeCore(library=[_skill()], **{"skill-update": gone})
    tab = make(core).agents.skills_tab
    tab.table.selectRow(0)
    tab.update_button.click()
    assert tab.message.text() == "git could not fetch it: timed out"
    assert tab.table.rowCount() == 1


def test_removing_asks_first_and_says_what_conversations_will_be_told(make, monkeypatch):
    asked = []
    monkeypatch.setattr(widgets, "confirm", lambda *args, **kw: asked.append(args) or True)
    core = FakeCore(library=[_skill(), _skill("test-writer")])
    tab = make(core).agents.skills_tab
    tab.table.selectRow(0)
    tab.remove_button.click()
    assert asked[0][2] == "Remove odoo-review?"
    assert "told it is gone when they next take a turn" in asked[0][3]
    assert ("skill-remove", "odoo-review") in [args for args, _doc in core.calls]
    assert [tab.table.item(row, 0).text() for row in range(tab.table.rowCount())] == [
        "test-writer"]


# -- choosing for a conversation -----------------------------------------------------
def _chat(make, core):
    page = make(core, tab="chat")
    return page, page.agents.chat


def test_the_composer_opens_the_library_ticked_for_this_conversation(make, qapp):
    core = FakeCore(library=[_skill(), _skill("test-writer", "Write edge-case tests.")],
                    own={"claude": ["pdf"]})
    page, chat = _chat(make, core)
    assert chat.composer.skills_button.text() == "Skills"
    assert "skills" not in core.asked()                  # not read until somebody asks
    chat.composer.skills_button.click()
    qapp.processEvents()
    panel = chat.skills_panel
    assert core.asked().count("skills") == 1 and "skill-look" not in core.asked()
    assert panel.isVisible()
    assert panel.list.values() == ["odoo-review", "test-writer"] and panel.list.checked() == []
    assert panel.note.text() == "The agent also has, in every turn: pdf."
    assert panel.list.count.text() == "0 of 2 skills"
    panel.hide()


def test_the_chooser_shows_a_line_a_skill_and_never_scrolls_sideways(make, qapp):
    long = "Write edge-case tests for the change being reviewed, then run them all."
    core = FakeCore(library=[_skill("test-writer", long)])
    page, chat = _chat(make, core)
    chat.composer.skills_button.click()
    qapp.processEvents()
    panel = chat.skills_panel
    item = panel.list.list.item(0)
    assert len(item.text()) <= 30 + 1 + agentskills.NOTE_CHARS
    assert item.text().rstrip().endswith("...") and item.toolTip() == long
    assert panel.list.list.horizontalScrollBar().isVisible() is False
    panel.hide()


def test_skills_ticked_before_the_first_message_go_with_the_conversation(make, qapp):
    core = FakeCore(library=[_skill(), _skill("test-writer")])
    page, chat = _chat(make, core)
    chat.composer.skills_button.click()
    chat.skills_panel.list.set_checked(["test-writer"], notify=True)
    chat.skills_panel.hide()
    assert chat.composer.skills_button.text() == "Skills: 1"
    assert chat.composer.skills_button.toolTip() == "The agent is handed: test-writer"
    assert "session-set" not in core.asked()             # nothing to save it on yet
    chat.composer.set_draft("review it")
    chat.composer.send()
    document = next(doc for args, doc in core.calls if args[0] == "session-new")
    assert document["skills"] == ["test-writer"]
    # The next new conversation starts with none.
    FakeTurn.made[0].say(_events("review it")[0])
    FakeTurn.made[0].finish()
    chat.new_button.click()
    assert chat.composer.skills() == [] and chat.composer.skills_button.text() == "Skills"


def test_skills_of_a_conversation_begun_are_saved_on_it_the_moment_they_are_ticked(make):
    session = _session(skills=["odoo-review"])
    core = FakeCore(library=[_skill(), _skill("test-writer")], sessions=[session],
                    events={session["id"]: _events()})
    page, chat = _chat(make, core)
    chat.table.selectRow(0)
    assert chat.composer.skills_button.text() == "Skills: 1"
    chat.composer.skills_button.click()
    assert chat.skills_panel.list.checked() == ["odoo-review"]
    chat.skills_panel.list.set_checked(["odoo-review", "test-writer"], notify=True)
    chat.skills_panel.hide()
    assert core.calls[-1] == (("session-set", session["id"]),
                              {"skills": ["odoo-review", "test-writer"]})
    assert chat.current["skills"] == ["odoo-review", "test-writer"]
    assert chat.composer.skills_button.text() == "Skills: 2"


def test_a_skill_chosen_and_since_removed_is_still_shown_so_it_can_be_unticked(make):
    session = _session(skills=["gone"])
    core = FakeCore(library=[_skill()], sessions=[session], events={session["id"]: _events()})
    page, chat = _chat(make, core)
    chat.table.selectRow(0)
    chat.composer.skills_button.click()
    assert chat.skills_panel.list.values() == ["odoo-review", "gone"]
    assert chat.skills_panel.list.checked() == ["gone"]
    chat.skills_panel.hide()


def test_a_skill_with_scripts_says_when_the_turn_will_not_let_them_run(make):
    core = FakeCore(library=[_skill(scripts=True), _skill("notes")])
    page, chat = _chat(make, core)
    chat.composer.skills_button.click()
    panel = chat.skills_panel
    assert not panel.note.isVisibleTo(panel)
    panel.list.set_checked(["notes"], notify=True)
    assert panel.note.text() == ""
    panel.list.set_checked(["notes", "odoo-review"], notify=True)
    assert panel.note.text() == ("odoo-review has scripts: the agent can read them, and runs "
                                 "them only with Full access.")
    panel.hide()
    chat.composer.mode.set_current("Full")
    chat.composer.skills_button.click()
    assert panel.note.text() == ""                       # Full lets them run
    panel.hide()


def test_an_empty_library_says_so_and_leads_to_adding_a_skill(make, monkeypatch):
    opened = []
    monkeypatch.setattr(AddSkillDialog, "exec",
                        lambda dialog: opened.append(dialog) or QDialog.Rejected)
    page, chat = _chat(make, FakeCore())
    chat.composer.skills_button.click()
    panel = chat.skills_panel
    assert panel.empty.isVisibleTo(panel) and not panel.list.isVisibleTo(panel)
    panel.add_button.click()
    assert not panel.isVisible()
    assert page.agents.tabs.currentWidget() is page.agents.skills_tab and len(opened) == 1


def test_a_first_message_that_could_not_be_said_keeps_the_skills_chosen_for_it(make):
    core = FakeCore(library=[_skill()])
    page, chat = _chat(make, core)
    chat.composer.skills_button.click()
    chat.skills_panel.list.set_checked(["odoo-review"], notify=True)
    chat.skills_panel.hide()
    chat.composer.set_draft("review it")
    chat.composer.send()
    FakeTurn.made[0].refuse("skills", "The skill 'odoo-review' is not in the library any more.")
    assert chat.current is None
    assert chat.composer.skills() == ["odoo-review"]
    assert "not in the library any more" in chat.composer.note.text()


def test_esc_closes_the_list_and_what_was_ticked_stays(make, qapp):
    from PySide6.QtTest import QTest
    core = FakeCore(library=[_skill()])
    page, chat = _chat(make, core)
    chat.composer.skills_button.click()
    chat.skills_panel.list.set_checked(["odoo-review"], notify=True)
    QTest.keyClick(chat.skills_panel, Qt.Key_Escape)
    assert not chat.skills_panel.isVisible()
    assert chat.composer.skills() == ["odoo-review"]
