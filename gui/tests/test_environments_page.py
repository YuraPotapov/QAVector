"""The Environments page: an environment is a name, its variables and its users.

Everything here is saved as it is made - a variable when its cell is left,
users when the picker closes - so each test changes something on the page and
then reads the files back.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QDialog, QLabel, QLineEdit

from cms_gui import launchprojectsfile as lpf, usersfile
from cms_gui.pages import environments as page_module
from cms_gui.pages.environmentdialog import UsersPicker
from cms_gui.pages.environments import EnvironmentsPage
from cms_gui.settings import Settings

LOCAL = "env:localhost:8069"
STAGING = "env:https://staging.example.invalid/"

USERS = [
    {"env": "localhost:8069", "class": "Agent", "login": "agent", "password": "pw",
     "run-tests": ["smoke"]},
    {"env": "localhost:8069", "class": "Admin", "login": "admin", "password": "pw"},
    {"env": "https://staging.example.invalid/", "class": "Agent", "login": "agent",
     "password": "pw", "run-tests": ["tag:access"]},
    {"env": "", "class": "Tester", "login": "new", "password": "pw"},
    {"env": "demo", "class": "Viewer", "login": "viewer", "password": "pw"},
]


class FakeInventory:
    def __init__(self, config_path=""):
        self.config_path = config_path
        self.envs = [
            {"alias": "local", "value": "localhost:8069",
             "origin": "http://localhost:8069", "count": 2},
            {"alias": "staging", "value": "https://staging.example.invalid/",
             "origin": "https://staging.example.invalid", "count": 1},
            {"alias": "demo", "value": "demo", "origin": "", "count": 1},
        ]

    def dirs(self):
        return {}


def _labels(widget):
    return "\n".join(label.text() for label in widget.findChildren(QLabel)
                     if label.isVisibleTo(widget))


def _users(path):
    return json.loads(open(path, encoding="utf-8").read())


@pytest.fixture
def files(tmp_path):
    users = tmp_path / "users.json"
    users.write_text(json.dumps(USERS), encoding="utf-8")
    return {"users": str(users), "projects": str(tmp_path / lpf.FILE_NAME),
            "settings": str(tmp_path / "gui.ini")}


@pytest.fixture
def page(qapp, files, dispose):
    settings = Settings()
    settings._qs = QSettings(files["settings"], QSettings.IniFormat)
    widget = EnvironmentsPage(settings, registry=lpf.Registry(files["projects"]))
    widget.resize(1200, 760)
    widget.inventory = FakeInventory(files["users"])
    widget.set_inventory(widget.inventory)
    yield widget
    dispose(widget)


def _say_yes(monkeypatch, **answers):
    """Every confirmation says yes; ask_text / ask_item answer from ``answers``."""
    from cms_gui import widgets
    monkeypatch.setattr(widgets, "confirm", lambda *a, **k: True)
    warned = []
    monkeypatch.setattr(widgets, "warn", lambda *a, **k: warned.append(a[2]))
    if "text" in answers:
        monkeypatch.setattr(widgets, "ask_text", lambda *a, **k: answers["text"])
    if "item" in answers:
        monkeypatch.setattr(widgets, "ask_item", lambda *a, **k: answers["item"])
    return warned


def _env(page, env_id):
    return lpf.find(page.registry.environments(page.inventory), env_id)


def _pick(page, name):
    for row in range(page.table.rowCount()):
        if page.table.item(row, 0).text() == name:
            page.table.selectRow(row)
            return
    raise AssertionError("no %s in the list" % name)


def _variables(page):
    return [(page.variables.item(row, 0).text(), page.variables.item(row, 1).text())
            for row in range(page.variables.rowCount())]


# ------------------------------------------------------------- what is shown

def test_every_users_json_value_is_an_environment_with_nothing_set_up(page):
    rows = [(page.table.item(row, 0).text(), page.table.item(row, 1).text())
            for row in range(page.table.rowCount())]
    assert rows == [("local", "2"), ("staging", "1"), ("demo", "1")]


def test_the_first_environment_is_open_with_url_first(page):
    assert page.editor_stack.currentIndex() == 1
    assert page.name_edit.text() == "local"
    assert _variables(page) == [("url", "http://localhost:8069")]
    assert page.opens.text() == "Opens http://localhost:8069"
    users = [(page.users.item(row, 0).text(), page.users.item(row, 1).text(),
              page.users.item(row, 2).text()) for row in range(page.users.rowCount())]
    assert users == [("Agent", "agent", "smoke"), ("Admin", "admin", "—")]


def test_nothing_internal_is_said_until_developer_mode(page):
    _pick(page, "staging")
    plain = _labels(page)
    for internal in ("Account key", "Origin", "env value", "--"):
        assert internal not in plain, internal
    page.set_developer_mode(True)
    assert "--env https://staging.example.invalid/" in _labels(page)
    assert "--url" in _labels(page)
    page.set_developer_mode(False)
    assert "--" not in _labels(page)


def test_an_environment_without_a_url_says_it_cannot_open(page):
    _pick(page, "demo")
    assert _variables(page) == [("url", "")]
    assert "No url yet" in page.opens.text()
    assert page.opens.property("role") == "error"


def test_nothing_selected_and_nothing_found_are_said(page):
    class Empty:
        envs = []
        config_path = ""

        def dirs(self):
            return {}

    page.search_row.box(0).setText("nothing like this")
    assert page.table.rowCount() == 3
    assert "matches the search" in page._env_note.text()
    page.search_row.box(0).setText("")
    page.set_inventory(Empty())
    assert page.table.rowCount() == 0
    assert page.editor_stack.currentIndex() == 0
    assert not page.add_variable_button.isEnabled()


# ------------------------------------------------------------- variables

def test_a_url_typed_is_written_down_and_announced(page):
    seen = []
    page.addresses_changed.connect(lambda: seen.append(True))
    page.variables.item(0, 1).setText("http://localhost:{{port}}/web")
    env = _env(page, LOCAL)
    assert (env.id, env.address, env.virtual) == (
        LOCAL, "http://localhost:{{port}}/web", False)
    assert seen == [True]
    assert "{{port}} has no value yet" in page.opens.text()
    assert page.address_for(LOCAL) == "http://localhost:{{port}}/web"


def test_a_variable_is_added_renamed_and_removed(page, monkeypatch):
    warned = _say_yes(monkeypatch)
    page.variables.item(0, 1).setText("http://localhost:{{port}}/web")
    page.add_variable()
    assert page.variables.rowCount() == 2
    page.variables.item(1, 0).setText("port")
    assert _env(page, LOCAL).variables == {"port": ""}
    page.variables.item(1, 1).setText("8074")
    assert _env(page, LOCAL).variables == {"port": "8074"}
    assert page.opens.text() == "Opens http://localhost:8074/web"

    page.variables.item(1, 0).setText("env")             # reserved: refused
    assert warned and page.variables.item(1, 0).text() == "port"
    page.variables.item(1, 0).setText("app_port")
    assert _env(page, LOCAL).variables == {"app_port": "8074"}
    assert "{{port}} has no value yet" in page.opens.text()

    page.variables.selectRow(1)
    page.remove_variable()
    assert _env(page, LOCAL).variables == {}
    assert _variables(page) == [("url", "http://localhost:{{port}}/web")]


def test_url_keeps_its_name_and_its_row(page):
    assert not page.variables.item(0, 0).flags() & Qt.ItemIsEditable
    assert page.variables.item(0, 1).flags() & Qt.ItemIsEditable
    page.variables.selectRow(0)
    assert not page.remove_variable_button.isEnabled()


def test_a_value_given_before_its_name_waits_for_the_name(page):
    page.add_variable()
    page.variables.item(1, 1).setText("8074")
    assert _env(page, LOCAL).virtual, "nothing is written without a name"
    page.variables.item(1, 0).setText("port")
    assert _env(page, LOCAL).variables == {"port": "8074"}


# ------------------------------------------------------------- users

def test_the_picker_ticks_who_is_here_and_says_where_everyone_else_is(page, dispose):
    picker = UsersPicker(page, _env(page, LOCAL), page._users_path(), page._env_names())
    try:
        text = [picker.list.list.item(i).text() for i in range(picker.list.list.count())]
        assert len(text) == 4            # agent is one person in two places
        assert "local, staging" in text[0] and "no environment" in text[2]
        assert picker.changes() == ([], [])
        picker.list.set_checked(["0", "2"])       # admin out, new in
        assert picker.changes() == ([("new", "pw", "Tester")], [("admin", "pw", "Admin")])
    finally:
        dispose(picker)


def test_one_login_cannot_be_ticked_twice_in_one_place(page, files, dispose):
    rows = _users(files["users"])
    rows.append({"env": "https://staging.example.invalid/", "class": "Admin",
                 "login": "admin", "password": "other"})
    open(files["users"], "w").write(json.dumps(rows))
    picker = UsersPicker(page, _env(page, STAGING), files["users"], {})
    try:
        items = [picker.list.list.item(i) for i in range(picker.list.list.count())]
        local_admin = [item for item in items if "already here" in item.text()]
        assert len(local_admin) == 1
        assert not local_admin[0].flags() & Qt.ItemIsEnabled
    finally:
        dispose(picker)


def test_users_chosen_in_the_picker_are_written_and_announced(page, files, monkeypatch):
    class Chosen:
        wrote = False

        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return QDialog.Accepted

        def changes(self):
            return [("new", "pw", "Tester")], [("admin", "pw", "Admin")]

    monkeypatch.setattr(page_module, "UsersPicker", Chosen)
    seen = []
    page.accounts_changed.connect(lambda: seen.append(True))
    page.add_users()
    assert seen == [True]
    assert [(row["env"], row["login"]) for row in _users(files["users"])] == [
        ("localhost:8069", "agent"), ("", "admin"),
        ("https://staging.example.invalid/", "agent"), ("localhost:8069", "new"),
        ("demo", "viewer")]
    assert [page.users.item(row, 1).text() for row in range(page.users.rowCount())] == \
        ["agent", "new"]


def test_removing_users_keeps_them_in_credentials(page, files, monkeypatch):
    _say_yes(monkeypatch)
    page.users.selectRow(0)
    page.remove_users()
    agent = [row for row in _users(files["users"]) if row["login"] == "agent"]
    assert [row["env"] for row in agent] == ["https://staging.example.invalid/"]
    page.users.selectRow(0)                  # admin, the only place it was in
    page.remove_users()
    admin = [row for row in _users(files["users"]) if row["login"] == "admin"]
    assert [row["env"] for row in admin] == [""]
    # Written down before its last user left, so it is still here to fill.
    env = _env(page, LOCAL)
    assert (env.virtual, env.address) == (False, "http://localhost:8069")
    assert page.name_edit.text() == "local"
    assert page.users.rowCount() == 0
    assert "No users here yet" in page._users_note.text()


def test_scenarios_are_changed_for_this_environment_only(page, files, qapp):
    page.users.item(0, 2).setText("smoke, tag:x")
    qapp.processEvents()
    agent = [row for row in _users(files["users"]) if row["login"] == "agent"]
    assert [row.get("run-tests") for row in agent] == [["smoke", "tag:x"], ["tag:access"]]
    assert page.users.item(0, 2).text() == "smoke, tag:x"


def test_an_unreadable_users_file_is_said_and_users_cannot_change(page, files):
    open(files["users"], "w").write("[broken")
    page.set_inventory(page.inventory)
    assert "cannot be read" in page.users_problem.text()
    assert page.users_problem.isVisibleTo(page)
    assert not page.add_users_button.isEnabled()


# ------------------------------------------------------------- environments

def test_an_environment_is_added_with_a_key_made_from_its_name(page, monkeypatch):
    class Form:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return QDialog.Accepted

        def result_values(self):
            return "Helpdesk", "http://localhost:{{port}}"

    monkeypatch.setattr(page_module, "EnvironmentDialog", Form)
    page.add_environment()
    env = [env for env in page._envs if env.name == "Helpdesk"][0]
    assert env.account_key == "helpdesk" and env.address == "http://localhost:{{port}}"
    assert page.name_edit.text() == "Helpdesk"
    assert page.users.rowCount() == 0


def test_a_rename_keeps_the_key_and_so_the_profiles(page, files):
    page.name_edit.setText("Local box")
    page.name_edit.editingFinished.emit()
    env = _env(page, LOCAL)
    assert (env.name, env.account_key) == ("Local box", "localhost:8069")
    assert page.table.item(0, 0).text() == "Local box"
    assert [row["env"] for row in _users(files["users"])][:2] == ["localhost:8069"] * 2


def test_duplicate_copies_variables_and_users_under_a_key_of_its_own(page, files,
                                                                     monkeypatch):
    _say_yes(monkeypatch, text="Local copy")
    page.variables.item(0, 1).setText("http://localhost:{{port}}")
    page.add_variable()
    page.variables.item(1, 0).setText("port")
    page.variables.item(1, 1).setText("8069")
    page.duplicate_environment()
    copy = [env for env in page._envs if env.name == "Local copy"][0]
    assert copy.account_key == "local-copy"
    assert (copy.address, copy.variables) == ("http://localhost:{{port}}",
                                              {"port": "8069"})
    copied = [(row["login"], row.get("run-tests")) for row in _users(files["users"])
              if row["env"] == "local-copy"]
    assert copied == [("agent", ["smoke"]), ("admin", None)]
    assert page.name_edit.text() == "Local copy"


def test_delete_takes_its_users_out_and_keeps_them(page, files, monkeypatch):
    _say_yes(monkeypatch)
    page.delete_environment()
    assert [(row["env"], row["login"]) for row in _users(files["users"])] == [
        ("", "admin"), ("https://staging.example.invalid/", "agent"), ("", "new"),
        ("demo", "viewer")]
    assert [env.name for env in page._envs] == ["staging", "demo"]


def test_project_buttons_ask_the_window(page):
    """Projects are the window's (one folder each): the page only asks."""
    asked = []
    page.project_action.connect(asked.append)
    for button in page.project_row.findChildren(type(page.add_env_button)):
        button.setEnabled(True)
        button.click()
    assert asked == ["add", "rename", "export", "delete"]


def test_an_unreadable_projects_file_is_said_and_nothing_can_be_changed(qapp, files,
                                                                        dispose):
    open(files["projects"], "w").write("{broken")
    settings = Settings()
    settings._qs = QSettings(files["settings"], QSettings.IniFormat)
    widget = EnvironmentsPage(settings, registry=lpf.Registry(files["projects"]))
    try:
        widget.set_inventory(FakeInventory(files["users"]))
        assert "cannot be read" in widget.registry_problem.text()
        assert not widget.add_env_button.isEnabled()
        assert not widget.add_variable_button.isEnabled()
        assert not widget.name_edit.isEnabled()
    finally:
        dispose(widget)


# ------------------------------------------------------------- old overrides

def test_an_old_override_is_offered_not_applied(page):
    page.settings._qs.setValue("env/override/local", "http://localhost:9000")
    page.set_inventory(page.inventory)
    assert page.legacy_box.isVisibleTo(page)
    assert page.address_for(LOCAL) == "", "an old override was switched on unasked"
    page._adopt_legacy("local", LOCAL, "http://localhost:9000")
    assert page.address_for(LOCAL) == "http://localhost:9000"
    assert page.settings.legacy_env_overrides() == {}
    assert not page.legacy_box.isVisibleTo(page)


def test_an_old_override_can_be_let_go(page):
    page.settings._qs.setValue("env/override/gone", "http://localhost:9001")
    page.set_inventory(page.inventory)
    page._drop_legacy("gone")
    assert page.settings.legacy_env_overrides() == {}


# ------------------------------------------------------------- search, layout

def test_search_lists_each_environment_and_reveal_selects_it(page):
    hits = page.search_rows()
    assert [h.title for h in hits] == ["local", "staging", "demo"]
    assert hits[1].detail == "https://staging.example.invalid · 1 user"
    assert page.show_found(STAGING)
    assert page.name_edit.text() == "staging"
    assert not page.show_found("gone")


def test_buttons_stand_level_with_the_control_beside_them(page, qapp):
    page.show()
    qapp.processEvents()
    for anchor, buttons in (
            (page.name_edit, (page.duplicate_button,
                              page.export_env_button, page.delete_env_button)),):
        centre = anchor.mapTo(page, anchor.rect().center()).y()
        for button in buttons:
            assert abs(button.mapTo(page, button.rect().center()).y() - centre) <= 1
            assert button.height() == anchor.height()


def test_the_tables_rows_are_one_height(page, qapp):
    page.show()
    qapp.processEvents()
    heights = {table.verticalHeader().defaultSectionSize()
               for table in (page.table, page.variables, page.users)}
    assert len(heights) == 1


def test_export_asks_for_the_environment_on_screen(page):
    asked = []
    page.export_requested.connect(asked.append)
    page.export_env_button.click()
    assert asked == [{"environments": [LOCAL]}]


# ------------------------------------------------------------- symmetry

def test_a_cell_editor_is_the_cell_exactly(page, qapp):
    """Not a form field dropped on the row: taller than it, shifted right."""
    page.show()
    qapp.processEvents()
    for table, row, col in ((page.variables, 0, 1), (page.users, 0, 2)):
        item = table.item(row, col)
        table.editItem(item)
        qapp.processEvents()
        editor = table.indexWidget(table.indexFromItem(item)) or next(
            child for child in table.viewport().findChildren(type(page.name_edit))
            if child.isVisible())
        assert editor.geometry() == table.visualItemRect(item)
        assert editor.property("role") == "celledit"
        table.closePersistentEditor(item)
        table.setFocus()
        qapp.processEvents()


def test_the_list_has_a_search_box_over_every_column(page, qapp):
    page.show()
    qapp.processEvents()
    header = page.table.horizontalHeader()
    for column in range(page.table.columnCount()):
        box = page.search_row.box(column)
        assert box is not None and box.isVisible()
        assert box.x() == header.sectionViewportPosition(column)
        assert box.width() == header.sectionSize(column) + 1


def test_url_is_marked_reserved(page):
    from cms_gui import theme
    for col in (0, 1):
        item = page.variables.item(0, col)
        assert "reserved variable" in item.toolTip()
        assert item.background().color() == theme.color(theme.TAG_TINT)
    page.add_variable()
    page.variables.item(1, 0).setText("port")
    assert page.variables.item(1, 0).toolTip() == ""


def test_a_variable_is_removed_by_button_and_by_delete(page, qapp):
    page.variables.item(0, 1).setText("http://localhost:{{port}}")
    page.add_variable()
    page.variables.item(1, 0).setText("port")
    page.variables.item(1, 1).setText("8069")
    page.show()
    qapp.processEvents()
    from PySide6.QtTest import QTest
    for editor in page.variables.viewport().findChildren(QLineEdit):
        if editor.isVisible():
            QTest.keyClick(editor, Qt.Key_Return)    # the name typed, Enter
    page.variables.selectRow(1)
    assert page.remove_variable_button.isEnabled()
    assert "Delete" in page.remove_variable_button.toolTip()
    page.variables.setFocus()
    qapp.processEvents()
    QTest.keyClick(page.variables, Qt.Key_Delete)
    assert _env(page, LOCAL).variables == {}
    page.variables.selectRow(0)
    assert not page.remove_variable_button.isEnabled()
    assert "reserved" in page.remove_variable_button.toolTip()


def test_a_row_not_named_yet_just_goes(page):
    page.add_variable()
    page.variables.closePersistentEditor(page.variables.item(1, 0))
    page.variables.setCurrentCell(1, 0)
    page.remove_variable()
    assert page.variables.rowCount() == 1 and _env(page, LOCAL).virtual
