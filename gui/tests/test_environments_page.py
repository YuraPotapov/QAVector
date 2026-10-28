"""The Environments page: the one row that holds a real control, and its wording.

Everything else on this page is text in a cell, which a row of any height can
draw. The URL override is an actual input sitting in a table, which is the one
arrangement where the design's own metrics and the view's own metrics have to be
made to agree.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QLabel

from cms_gui.pages.environments import EnvironmentsPage
from cms_gui.settings import Settings

URL_OVERRIDE = 4          # the column the editor lives in


class FakeInventory:
    envs = [
        {"alias": "local", "value": "localhost:8069",
         "origin": "http://localhost:8069", "count": 11},
        {"alias": "staging", "value": "https://staging.example.invalid/",
         "origin": "https://staging.example.invalid", "count": 10},
        {"alias": "demo", "value": "https://demo.example.invalid/",
         "origin": "", "count": 10},
    ]

    def dirs(self):
        return {}


@pytest.fixture
def page(qapp):
    widget = EnvironmentsPage(Settings())
    widget.resize(1100, 620)
    widget.set_inventory(FakeInventory())
    widget.show()
    qapp.processEvents()
    yield widget
    widget.close()


def _labels(widget):
    return "\n".join(label.text() for label in widget.findChildren(QLabel))


# ------------------------------------------------------------- the odd row out

def test_the_url_override_sits_squarely_inside_its_row(page):
    """It used to hang across the gridline below it.

    The design's inputs are 30px and a row of text is 30px, but a view insets a
    cell widget by the item padding on both sides - so the editor was handed 24,
    rendered at its own minimum anyway, and the extra six went downwards: 3px of
    air above it and 3px of it lying over the divider. Visibly lopsided, and only
    on this one column, which is what made it look like a mistake in the column
    rather than in the row.
    """
    table = page.table
    assert table.rowCount() == len(FakeInventory.envs)
    for row in range(table.rowCount()):
        editor = table.cellWidget(row, URL_OVERRIDE)
        top = table.rowViewportPosition(row)
        above = editor.y() - top
        below = (top + table.rowHeight(row)) - (editor.y() + editor.height())
        assert above == below, ("row %d: %dpx above, %dpx below"
                                % (row, above, below))
        assert above >= 0, "row %d starts above its own row" % row


def test_the_row_is_tall_enough_for_the_editor_rather_than_the_other_way_round(page):
    """The input keeps the height every other input in the app has."""
    table = page.table
    for row in range(table.rowCount()):
        editor = table.cellWidget(row, URL_OVERRIDE)
        assert editor.height() >= editor.minimumSizeHint().height(), row


def test_an_empty_inventory_leaves_the_table_alone(page):
    """No rows, nothing to measure, and no exception on the way through."""
    class Empty:
        envs = []

        def dirs(self):
            return {}

    page.set_inventory(Empty())
    assert page.table.rowCount() == 0


# ------------------------------------------------------------------- wording

def test_the_page_names_no_flag_until_developer_mode(page):
    """A flag is the answer to a question a regular user is not asking."""
    page.set_developer_mode(False)
    plain = _labels(page)
    assert "--" not in plain
    # And it still says what the controls do.
    assert "Flows" in plain and "Reports" in plain and "Sessions" in plain

    page.set_developer_mode(True)
    spelled_out = _labels(page)
    for flag in ("--describe", "--flows-dir", "--reports-dir", "--sessions-dir"):
        assert flag in spelled_out, flag


def test_the_wording_goes_back_when_the_mode_does(page):
    page.set_developer_mode(True)
    page.set_developer_mode(False)
    assert "--" not in _labels(page)


# ------------------------------------------------------------------ search (Ctrl+K)
def test_search_lists_each_environment_and_reveal_selects_it(page):
    hits = page.search_rows()
    assert [h.title for h in hits] == ["local", "staging", "demo"]
    assert hits[1].detail == "https://staging.example.invalid · 10 accounts"
    assert page.show_found("demo")
    assert [index.row() for index in page.table.selectionModel().selectedRows()] == [2]
    assert not page.show_found("gone")


# ------------------------------------------------------------- the address

@pytest.fixture
def isolated(qapp, tmp_path):
    """A page on settings and a projects file of its own."""
    from PySide6.QtCore import QSettings
    from cms_gui import launchprojectsfile as lpf
    settings = Settings()
    settings._qs = QSettings(str(tmp_path / "gui.ini"), QSettings.IniFormat)
    widget = EnvironmentsPage(settings,
                              registry=lpf.Registry(str(tmp_path / lpf.FILE_NAME)))
    widget.resize(1100, 620)
    widget.set_inventory(FakeInventory())
    yield widget
    widget.close()


def test_an_address_is_written_down_for_its_environment_and_announced(isolated):
    seen = []
    isolated.addresses_changed.connect(lambda: seen.append(True))
    editor = isolated.table.cellWidget(0, URL_OVERRIDE)
    assert editor.placeholderText() == "http://localhost:8069"
    editor.setText("http://localhost:{{port}}")
    editor.editingFinished.emit()
    env = isolated.registry.environments(FakeInventory())[0]
    assert (env.id, env.address, env.virtual) == (
        "env:localhost:8069", "http://localhost:{{port}}", False)
    assert seen == [True]
    editor.editingFinished.emit()           # nothing new: nothing announced
    assert seen == [True]
    assert isolated.address_for("env:localhost:8069") == "http://localhost:{{port}}"


def test_every_users_json_value_is_listed_with_nothing_set_up(isolated):
    names = [isolated.table.item(row, 0).text() for row in range(isolated.table.rowCount())]
    assert names == ["local", "staging", "demo"]
    assert isolated.table.item(0, 1).text() == "localhost:8069"


def test_an_old_override_is_offered_not_applied(isolated):
    settings = isolated.settings
    settings._qs.setValue("env/override/local", "http://localhost:9000")
    isolated.set_inventory(FakeInventory())
    assert isolated.legacy_box.isVisibleTo(isolated)
    assert isolated.address_for("env:localhost:8069") == "", \
        "an old override was switched on unasked"
    assert "never applied" in _labels(isolated.legacy_box)

    isolated._adopt_legacy("local", "env:localhost:8069", "http://localhost:9000")
    assert isolated.address_for("env:localhost:8069") == "http://localhost:9000"
    assert settings.legacy_env_overrides() == {}
    assert not isolated.legacy_box.isVisibleTo(isolated)


def test_an_old_override_can_be_let_go(isolated):
    settings = isolated.settings
    settings._qs.setValue("env/override/gone", "http://localhost:9001")
    isolated.set_inventory(FakeInventory())
    isolated._drop_legacy("gone")
    assert settings.legacy_env_overrides() == {}
    assert isolated.address_for("env:localhost:8069") == ""


# ------------------------------------------------------------- the editor

def _accept(monkeypatch, **answers):
    """Every dialog says yes; ask_text / ask_item answer from ``answers``."""
    from cms_gui import widgets
    monkeypatch.setattr(widgets, "confirm", lambda *a, **k: True)
    if "text" in answers:
        monkeypatch.setattr(widgets, "ask_text", lambda *a, **k: answers["text"])
    if "item" in answers:
        monkeypatch.setattr(widgets, "ask_item", lambda *a, **k: answers["item"])


def _select(page, row):
    page.table.selectRow(row)


def test_a_project_is_added_renamed_and_deleted_without_losing_anything(isolated,
                                                                         monkeypatch):
    _accept(monkeypatch, text="Helpdesk")
    assert isolated.project_hint.isVisibleTo(isolated)
    isolated.add_project()
    assert isolated.project_combo.currentText() == "Helpdesk"
    assert isolated.table.rowCount() == 0, "a new project starts empty"
    assert not isolated.project_hint.isVisibleTo(isolated)
    _accept(monkeypatch, text="Help desk")
    isolated.rename_project()
    assert isolated.project_combo.currentText() == "Help desk"
    isolated.delete_project()
    assert isolated.project_combo.currentData() == "workspace"
    assert [p.name for p in isolated.registry.projects] == ["Workspace"]


def test_an_environment_moves_to_a_project_with_the_variables_it_uses(isolated,
                                                                       monkeypatch):
    from cms_gui import launchprojectsfile as lpf
    _accept(monkeypatch, text="Helpdesk")
    isolated.add_project()
    isolated._project_id = "workspace"
    isolated.set_inventory_rows()
    isolated.registry.update(lambda projects: lpf.define(projects, "workspace", "port",
                                                         "port", 8069))
    isolated.set_inventory_rows()
    _select(isolated, 0)
    _accept(monkeypatch, item="Helpdesk")
    isolated.extract_port()
    _select(isolated, 0)
    isolated.move_environment()
    helpdesk = next(p for p in isolated.registry.projects if p.name == "Helpdesk")
    assert [row.id for row in helpdesk.environments] == ["env:localhost:8069"]
    assert "port" in helpdesk.variables
    assert helpdesk.environments[0].address == "http://localhost:{{port}}"


def test_extract_port_turns_the_port_into_a_variable(isolated, monkeypatch):
    _accept(monkeypatch)
    _select(isolated, 0)
    isolated.extract_port()
    env = isolated.registry.environments(FakeInventory())[0]
    assert env.address == "http://localhost:{{port}}" and env.variables == {"port": 8069}
    assert isolated.registry.project("workspace").definitions()["port"].type == "port"
    assert isolated.table.item(0, 5).text() == "port=8069"


def test_an_address_with_no_port_has_nothing_to_extract(isolated, monkeypatch):
    from cms_gui import widgets
    said = []
    monkeypatch.setattr(widgets, "note", lambda *a, **k: said.append(a))
    _select(isolated, 1)                         # https://staging... - no port
    isolated.extract_port()
    assert said and isolated.registry.projects == []


def test_an_environment_is_added_through_its_form(isolated, monkeypatch):
    from cms_gui.pages import environments as page_module

    class Form:
        def __init__(self, *_a, **_k):
            pass

        def exec(self):
            return page_module.QDialog.Accepted

        def result_values(self):
            return "Branch 42", "localhost:8069", "http://localhost:8142/web", {}
    monkeypatch.setattr(page_module, "EnvironmentDialog", Form)
    isolated.add_environment()
    names = [isolated.table.item(row, 0).text() for row in range(isolated.table.rowCount())]
    assert "Branch 42" in names


def test_a_virtual_environment_cannot_be_deleted_only_forgotten_once_written(
        isolated, monkeypatch):
    _select(isolated, 0)
    assert not isolated.delete_env_button.isEnabled()
    _accept(monkeypatch)
    isolated.extract_port()                      # written down now
    _select(isolated, 0)
    assert isolated.delete_env_button.isEnabled()
    isolated.delete_environment()
    env = isolated.registry.environments(FakeInventory())[0]
    assert env.virtual and env.address == ""


def test_a_variable_is_added_redefined_and_removed(isolated, monkeypatch):
    from cms_gui.pages import environments as page_module

    class Form:
        answer = ("host", "string", "localhost")

        def __init__(self, *_a, **_k):
            pass

        def exec(self):
            return page_module.QDialog.Accepted

        def result_values(self):
            return Form.answer
    monkeypatch.setattr(page_module, "VariableDialog", Form)
    isolated.add_variable()
    assert isolated.variables.rowCount() == 1
    assert isolated.variables.item(0, 2).text() == "localhost"
    isolated.variables.selectRow(0)
    Form.answer = ("host", "string", "127.0.0.1")
    isolated.edit_variable()
    assert isolated.variables.item(0, 2).text() == "127.0.0.1"
    isolated.variables.selectRow(0)
    _accept(monkeypatch)
    isolated.remove_variable()
    assert isolated.variables.rowCount() == 0


def test_an_unreadable_projects_file_is_said_and_nothing_can_be_changed(qapp, tmp_path):
    from PySide6.QtCore import QSettings
    from cms_gui import launchprojectsfile as lpf
    path = tmp_path / lpf.FILE_NAME
    path.write_text("{broken", encoding="utf-8")
    settings = Settings()
    settings._qs = QSettings(str(tmp_path / "gui.ini"), QSettings.IniFormat)
    widget = EnvironmentsPage(settings, registry=lpf.Registry(str(path)))
    try:
        widget.set_inventory(FakeInventory())
        assert widget.registry_problem.isVisibleTo(widget) or \
            widget.registry_problem.text()
        assert "cannot be read" in widget.registry_problem.text()
        assert not widget.add_env_button.isEnabled()
        assert not widget.add_variable_button.isEnabled()
    finally:
        widget.close()


def test_the_project_buttons_stand_level_with_the_combo(isolated, qapp):
    isolated.show()
    qapp.processEvents()
    combo = isolated.project_combo
    centre = combo.mapTo(isolated, combo.rect().center()).y()
    for button in (isolated.rename_project_button, isolated.delete_project_button):
        assert abs(button.mapTo(isolated, button.rect().center()).y() - centre) <= 1
