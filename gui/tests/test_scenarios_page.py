"""The Scenarios page: what it shows, what it lets you change, what it sends.

The page owns no file format - the core does - so what is worth testing here is
the boundary: that a bundled scenario cannot be edited by accident, that the two
views disagree in a defined way (whichever was touched last wins), and that the
document handed to the core is the one on screen.

The core is faked throughout. Its real behaviour is covered by
tests/test_flowfile.py and tests/test_session_launcher.py; repeating it through a
subprocess here would make these slow and prove nothing extra.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cms_gui import core as core_mod
from cms_gui.pages.scenarios import ScenariosPage
from cms_gui.settings import Settings


class FakeCore:
    """Records what the page asked for, and answers with what it was given."""

    def __init__(self, flows=None):
        self.flows = flows or {}
        self.saved = []
        self.deleted = []
        self.imported = []
        self.save_result = None
        self.selectors_yaml = ""

    def flow_show(self, flow_id):
        return self.flows[flow_id]

    def flow_save(self, flow_id, document):
        self.saved.append((flow_id, document))
        if self.save_result:
            return self.save_result
        # The real core can be asked for what it just wrote, and the page does
        # exactly that - it re-opens from disk rather than trusting the editor.
        self.flows[flow_id] = _flow(flow_id, yaml_text=document.get("yaml"))
        return {"ok": True, "id": flow_id, "path": "/tmp/%s.yaml" % flow_id,
                "problems": []}

    def flow_delete(self, flow_id):
        self.deleted.append(flow_id)
        return {"ok": True, "id": flow_id, "problems": []}

    def flow_import(self, path):
        self.imported.append(path)
        return {"ok": True, "id": "imported", "problems": []}

    def selectors_show(self):
        return {"path": "/flows/selectors.yaml", "writable": True,
                "yaml": self.selectors_yaml, "entries": {}, "problems": []}

    def selectors_save(self, yaml_text):
        self.selectors_yaml = yaml_text
        return {"ok": True, "path": "/flows/selectors.yaml", "problems": []}


def _flow(flow_id, writable=True, steps=None, yaml_text=None, **extra):
    payload = {
        "id": flow_id, "path": "/flows/scenarios/%s.yaml" % flow_id,
        "writable": writable, "source": "user" if writable else "bundled",
        "yaml": yaml_text or "id: %s\ntags: []\nsteps:\n  - click: \"x\"\n" % flow_id,
        "meta": {"id": flow_id, "name": flow_id.title(), "description": "",
                 "tags": ["smoke"]},
        "steps": steps if steps is not None else [{"action": "click", "target": "x"}],
        "problems": [], "unresolved": {"use": [], "selectors": []},
    }
    payload.update(extra)
    return payload


def _inventory(*rows, blocks=(), selectors=None):
    return core_mod.Inventory({
        "scenarios": list(rows),
        "blocks": list(blocks),
        "selectors": dict(selectors or {"menu_settings": ".o_menu_settings",
                                        "dashboard": ".o_main_navbar"}),
        "flow_actions": {"selector_only": ["click", "assert_visible"],
                         "selector_and_value": ["fill"],
                         "value_only": ["press"], "url_target": ["goto"],
                         "use": ["use"], "states": ["visible", "detached"]},
        "flows_dir": "/home/u/QAVector/flows",
    })


def _row(flow_id, writable=True, name=None, tags=("smoke",)):
    return {"id": flow_id, "name": name or flow_id.title(), "tags": list(tags),
            "writable": writable, "source": "user" if writable else "bundled",
            "path": "/flows/scenarios/%s.yaml" % flow_id, "in_all": True}


@pytest.fixture
def page(qapp):
    page = ScenariosPage(Settings())
    yield page
    page.close()


# ------------------------------------------------------------------- the list

def test_the_list_shows_every_scenario(page):
    page.set_inventory(_inventory(_row("alpha"), _row("beta", writable=False)))
    assert page.table.rowCount() == 2
    assert page.table.item(0, 0).text() == "alpha"
    # The column marks the exception: a bundled scenario is the ordinary case and
    # says nothing, one you can edit says so.
    assert page.table.item(0, 2).text() == "yours"
    assert page.table.item(1, 2).text() == ""


def test_the_search_box_hides_what_does_not_match(page):
    page.set_inventory(_inventory(_row("alpha"), _row("beta")))
    page.search.setText("bet")
    assert page.table.isRowHidden(0) and not page.table.isRowHidden(1)


def test_the_lede_names_where_scenarios_are_written(page):
    page.set_inventory(_inventory(_row("alpha")))
    assert "/home/u/QAVector/flows" in page.lede.text()


# ---------------------------------------------------------------- opening one

def test_opening_fills_both_views(page):
    page.core = FakeCore({"alpha": _flow("alpha")})
    page.set_inventory(_inventory(_row("alpha")))
    page.open("alpha")
    assert page.name_edit.text() == "Alpha"
    assert page.tags_edit.text() == "smoke"
    assert page.steps.rowCount() == 1
    assert page.steps.cellWidget(0, 0).currentText() == "click"
    assert page.steps.item(0, 1).text() == "x"
    assert "steps:" in page.yaml.toPlainText()
    assert not page.is_dirty()


def test_a_bundled_scenario_opens_read_only(page):
    # It is replaced on the next upgrade, so editing it in place would lose the
    # work silently. Duplicate is offered instead.
    page.core = FakeCore({"shipped": _flow("shipped", writable=False)})
    page.set_inventory(_inventory(_row("shipped", writable=False)))
    page.open("shipped")
    assert page.yaml.isReadOnly() and page.name_edit.isReadOnly()
    assert not page.save_button.isEnabled()
    assert not page.delete_button.isEnabled()
    assert page.duplicate_button.isEnabled()


def test_problems_are_shown_rather_than_hidden(page):
    page.core = FakeCore({"bad": _flow("bad", problems=["unknown action 'wat'"])})
    page.set_inventory(_inventory(_row("bad")))
    page.open("bad")
    assert "unknown action" in page.problems.text()


def test_unresolved_references_read_as_a_warning_not_a_failure(page):
    page.core = FakeCore({"refs": _flow(
        "refs", unresolved={"use": [], "selectors": ["not_a_name"]})})
    page.set_inventory(_inventory(_row("refs")))
    page.open("refs")
    assert "not_a_name" in page.problems.text()
    assert "selectors.yaml" in page.problems.text()


def test_a_step_whose_action_the_core_no_longer_knows_is_kept(page):
    # Dropping it would silently rewrite the scenario the moment it was opened.
    page.core = FakeCore({"old": _flow("old", steps=[{"action": "teleport",
                                                      "target": "x"}])})
    page.set_inventory(_inventory(_row("old")))
    page.open("old")
    assert page.steps.cellWidget(0, 0).currentText() == "teleport"


# ------------------------------------------------------------------- editing

def test_editing_marks_the_save_button(page):
    page.core = FakeCore({"alpha": _flow("alpha")})
    page.set_inventory(_inventory(_row("alpha")))
    page.open("alpha")
    page.name_edit.setText("Something else")
    assert page.is_dirty()
    assert page.save_button.property("dirty") == "true"


def test_going_back_to_the_saved_value_settles_it_again(page):
    page.core = FakeCore({"alpha": _flow("alpha")})
    page.set_inventory(_inventory(_row("alpha")))
    page.open("alpha")
    page.name_edit.setText("Something else")
    page.name_edit.setText("Alpha")
    assert not page.is_dirty()


def test_the_inputs_in_a_step_row_sit_inside_it(page, qapp):
    """The action box and the editor a cell opens into both fit their row.

    A row sized for text left both 3px down and hanging over the gridline
    beneath: the view insets a widget in a cell by the item padding on both
    sides (theme.CELL_INSET_V), and neither will draw shorter than its minimum.
    """
    from PySide6.QtWidgets import QLineEdit

    page.core = FakeCore({"alpha": _flow("alpha", steps=[
        {"action": "click", "target": "first"},
        {"action": "click", "target": "second"}])})
    page.set_inventory(_inventory(_row("alpha")))
    page.open("alpha")
    page.resize(1200, 800)
    page.show()
    qapp.processEvents()
    table = page.steps

    def centred(cell, box):
        above, below = box.top() - cell.top(), cell.bottom() - box.bottom()
        return cell.contains(box) and abs(above - below) <= 1

    for row in range(table.rowCount()):
        cell = table.visualRect(table.model().index(row, 0))
        assert centred(cell, table.cellWidget(row, 0).geometry()), row

    table.editItem(table.item(1, 1))
    qapp.processEvents()
    editor = table.viewport().findChild(QLineEdit)
    assert editor is not None
    assert centred(table.visualRect(table.model().index(1, 1)), editor.geometry())


def test_adding_and_moving_steps(page):
    page.core = FakeCore({"alpha": _flow("alpha", steps=[
        {"action": "click", "target": "first"},
        {"action": "click", "target": "second"}])})
    page.set_inventory(_inventory(_row("alpha")))
    page.open("alpha")
    page.steps.selectRow(0)
    page.move_step(1)
    assert [s["target"] for s in page.step_rows()] == ["second", "first"]
    page.add_step()
    assert page.steps.rowCount() == 3


def test_a_timeout_is_only_carried_when_it_is_a_number(page):
    page.core = FakeCore({"alpha": _flow("alpha")})
    page.set_inventory(_inventory(_row("alpha")))
    page.open("alpha")
    page.steps.item(0, 3).setText("not a number")
    assert "timeout" not in page.step_rows()[0]
    page.steps.item(0, 3).setText("2500")
    assert page.step_rows()[0]["timeout"] == 2500


# ------------------------------------------------------------------- saving

def test_saving_sends_the_steps_when_the_steps_were_edited(page):
    page.core = FakeCore({"alpha": _flow("alpha")})
    page.set_inventory(_inventory(_row("alpha")))
    page.open("alpha")
    page.name_edit.setText("Renamed")
    page.save()
    flow_id, document = page.core.saved[-1]
    assert flow_id == "alpha"
    assert document["meta"]["name"] == "Renamed"
    assert document["steps"][0]["action"] == "click"


def test_saving_sends_the_text_when_the_text_was_edited(page):
    # The two views can disagree - the grammar has corners the form cannot
    # express - so whichever was touched last is the one that gets written.
    page.core = FakeCore({"alpha": _flow("alpha")})
    page.set_inventory(_inventory(_row("alpha")))
    page.open("alpha")
    page.yaml.setPlainText("id: alpha\ntags: []\nsteps:\n  - press: \"Enter\"\n")
    page.save()
    _flow_id, document = page.core.saved[-1]
    assert "press" in document["yaml"]
    assert "steps" not in document


def test_saving_a_bundled_scenario_does_nothing(page):
    page.core = FakeCore({"shipped": _flow("shipped", writable=False)})
    page.set_inventory(_inventory(_row("shipped", writable=False)))
    page.open("shipped")
    page.save()
    assert page.core.saved == []


def test_a_refused_save_leaves_the_editor_alone(page, monkeypatch):
    from cms_gui import widgets

    monkeypatch.setattr(widgets, "warn", lambda *a, **k: None)
    page.core = FakeCore({"alpha": _flow("alpha")})
    page.core.save_result = {"ok": False, "id": "alpha",
                             "problems": ["unknown action 'wat'"]}
    page.set_inventory(_inventory(_row("alpha")))
    page.open("alpha")
    page.name_edit.setText("Renamed")
    page.save()
    assert page.name_edit.text() == "Renamed"   # not reset behind the user


def test_saving_settles_the_page(page, monkeypatch):
    """After a save there is nothing unsaved, and nothing may say otherwise.

    The page re-selects the row it just wrote, and re-selecting used to run
    straight into "discard your changes?" - immediately after saving them.
    """
    from cms_gui import widgets

    def refuse(*_a, **_k):
        raise AssertionError("asked to discard changes right after saving them")

    page.core = FakeCore({"alpha": _flow("alpha")})
    page.set_inventory(_inventory(_row("alpha")))
    page.open("alpha")
    page.name_edit.setText("Renamed")
    monkeypatch.setattr(widgets, "confirm", refuse)
    page.save()
    assert not page.is_dirty()
    assert page.save_button.property("dirty") == "false"


def test_saving_re_opens_from_disk(page):
    # The core may have sanitised the id, and the YAML view should show the file
    # as it was actually written rather than what was typed.
    page.core = FakeCore({"alpha": _flow("alpha")})
    page.set_inventory(_inventory(_row("alpha")))
    page.open("alpha")
    page.yaml.setPlainText("id: alpha\ntags: []\nsteps: []\n")
    page.save()
    assert page.current is not None and page.current["id"] == "alpha"


def test_saving_announces_itself_so_the_window_re_reads_describe(page):
    page.core = FakeCore({"alpha": _flow("alpha")})
    page.set_inventory(_inventory(_row("alpha")))
    page.open("alpha")
    seen = []
    page.saved.connect(lambda: seen.append(True))
    page.save()
    assert seen == [True]


# -------------------------------------------------------- new, duplicate, etc.

def test_a_new_scenario_starts_as_a_template(page, monkeypatch):
    # So a half-written one is not picked up by --run-tests=all.
    from PySide6.QtWidgets import QInputDialog

    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("fresh", True))
    page.core = FakeCore()
    page.set_inventory(_inventory())
    page.new_scenario()
    flow_id, document = page.core.saved[-1]
    assert flow_id == "fresh"
    assert document["meta"]["tags"] == ["template"]
    assert document["steps"][0] == {"action": "use", "target": "auth.login"}


def test_duplicating_sends_the_text_of_the_original(page, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("copy", True))
    page.core = FakeCore({"shipped": _flow("shipped", writable=False)})
    page.set_inventory(_inventory(_row("shipped", writable=False)))
    page.open("shipped")
    page.duplicate_scenario()
    flow_id, document = page.core.saved[-1]
    assert flow_id == "copy"
    assert document["yaml"] == page.core.flows["shipped"]["yaml"]


def test_deleting_asks_first(page, monkeypatch):
    from cms_gui import widgets

    monkeypatch.setattr(widgets, "confirm", lambda *a, **k: False)
    page.core = FakeCore({"alpha": _flow("alpha")})
    page.set_inventory(_inventory(_row("alpha")))
    page.open("alpha")
    page.delete_scenario()
    assert page.core.deleted == []

    monkeypatch.setattr(widgets, "confirm", lambda *a, **k: True)
    page.delete_scenario()
    assert page.core.deleted == ["alpha"]


def test_exporting_writes_the_file(page, tmp_path):
    page.core = FakeCore({"alpha": _flow("alpha")})
    page.set_inventory(_inventory(_row("alpha")))
    page.open("alpha")
    target = tmp_path / "alpha.yaml"
    assert page.write_to(str(target))
    assert target.read_text(encoding="utf-8") == page.core.flows["alpha"]["yaml"]


def test_exporting_somewhere_unwritable_reports_failure(page):
    page.core = FakeCore({"alpha": _flow("alpha")})
    page.set_inventory(_inventory(_row("alpha")))
    page.open("alpha")
    assert not page.write_to("/proc/definitely/not/here.yaml")


# ------------------------------------------------------------ the action menu

def test_the_action_menu_comes_from_the_core(page):
    page.set_inventory(_inventory(_row("alpha")))
    assert page.actions_for("selector_and_value") == ["fill"]
    assert "goto" in page.all_actions() and "use" in page.all_actions()


def test_the_action_menu_falls_back_for_an_older_core(page):
    page.set_inventory(core_mod.Inventory({"scenarios": []}))
    assert "click" in page.all_actions()
    assert "assert_text_contains" in page.all_actions()


# ------------------------------------------------- following what an alias means
# A step's target is an alias: another flow's id, or a name from selectors.yaml.
# Being able to read one without reaching what it stands for is most of what made
# the flow tree hard to learn, so the page has to lead somewhere in both cases.

def test_blocks_are_listed_alongside_scenarios(page):
    page.set_inventory(_inventory(_row("alpha"),
                                  blocks=[_row("auth.login", writable=False)]))
    assert page.table.rowCount() == 2
    assert page.table.item(1, 0).text() == "auth.login"
    assert page.table.item(1, 2).text() == "block"


def test_a_use_step_opens_the_block_it_names(page):
    page.core = FakeCore({"alpha": _flow("alpha", steps=[{"action": "use",
                                                          "target": "auth.login"}]),
                          "auth.login": _flow("auth.login", writable=False)})
    page.set_inventory(_inventory(_row("alpha"),
                                  blocks=[_row("auth.login", writable=False)]))
    page.open("alpha")
    page.steps.selectRow(0)
    page.open_target()
    assert page.current["id"] == "auth.login"


def test_a_selector_target_leads_to_the_selectors_tab(page):
    page.core = FakeCore({"alpha": _flow("alpha", steps=[{"action": "click",
                                                          "target": "menu_settings"}])})
    page.set_inventory(_inventory(_row("alpha")))
    page.open("alpha")
    page.steps.selectRow(0)
    page.open_target()
    assert page.left.currentIndex() == 1
    assert page.selector_search.text() == "menu_settings"


def test_the_steps_table_says_what_a_name_resolves_to(page):
    page.core = FakeCore({"alpha": _flow("alpha", steps=[
        {"action": "click", "target": "menu_settings"},
        {"action": "click", "target": ".raw-css"},
        {"action": "use", "target": "auth.login"}])})
    page.set_inventory(_inventory(_row("alpha")))
    page.open("alpha")
    resolved = [page.steps.item(row, 4).text() for row in range(3)]
    assert resolved == [".o_menu_settings", "raw selector", "block"]


def test_the_resolved_column_follows_an_edit(page):
    page.core = FakeCore({"alpha": _flow("alpha", steps=[{"action": "click",
                                                          "target": "nope"}])})
    page.set_inventory(_inventory(_row("alpha")))
    page.open("alpha")
    assert page.steps.item(0, 4).text() == "raw selector"
    page.steps.item(0, 1).setText("dashboard")
    assert page.steps.item(0, 4).text() == ".o_main_navbar"


# ------------------------------------------------------------------- selectors

def test_the_selector_list_shows_every_name(page):
    page.set_inventory(_inventory(_row("alpha")))
    names = [page.selector_table.item(row, 0).text()
             for row in range(page.selector_table.rowCount())]
    assert names == ["dashboard", "menu_settings"]
    assert page.selector_table.item(0, 1).text() == ".o_main_navbar"


def test_selectors_can_be_edited_and_saved(page):
    page.core = FakeCore()
    page.set_inventory(_inventory(_row("alpha")))
    page.load_selectors()
    page.selector_yaml.setPlainText('dashboard: ".mine"\n')
    page.save_selectors()
    assert page.core.selectors_yaml == 'dashboard: ".mine"\n'


def test_overriding_copies_a_bundled_name_into_your_own_file(page):
    # The way to change a name the app ships: take a copy, then edit it.
    page.core = FakeCore()
    page.set_inventory(_inventory(_row("alpha")))
    page.load_selectors()
    page.selector_table.selectRow(1)          # menu_settings
    page.override_selector()
    assert 'menu_settings: ".o_menu_settings"' in page.selector_yaml.toPlainText()


def test_overriding_the_same_name_twice_says_so(page):
    page.core = FakeCore()
    page.set_inventory(_inventory(_row("alpha")))
    page.load_selectors()
    page.selector_table.selectRow(1)
    page.override_selector()
    page.override_selector()
    assert "already one of yours" in page.selector_problems.text()
    defined = [line for line in page.selector_yaml.toPlainText().splitlines()
               if line.startswith("menu_settings:")]
    assert len(defined) == 1


def test_a_bad_selector_file_reports_rather_than_saving(page):
    page.core = FakeCore()
    page.core.selectors_save = lambda text: {"ok": False, "path": "/x",
                                             "problems": ["must be a mapping"]}
    page.set_inventory(_inventory(_row("alpha")))
    page.load_selectors()
    page.selector_yaml.setPlainText("- not a mapping\n")
    page.save_selectors()
    assert "must be a mapping" in page.selector_problems.text()


# ---------------------------------------------------------------------- revert

def test_revert_throws_the_edits_away(page):
    page.core = FakeCore({"alpha": _flow("alpha")})
    page.set_inventory(_inventory(_row("alpha")))
    page.open("alpha")
    page.name_edit.setText("Renamed")
    assert page.is_dirty() and page.revert_button.isEnabled()
    page.revert()
    assert page.name_edit.text() == "Alpha"
    assert not page.is_dirty()


def test_revert_is_only_offered_when_there_is_something_to_revert(page):
    page.core = FakeCore({"alpha": _flow("alpha")})
    page.set_inventory(_inventory(_row("alpha")))
    assert not page.revert_button.isEnabled()
    page.open("alpha")
    assert not page.revert_button.isEnabled()
