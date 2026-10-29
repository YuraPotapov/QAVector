"""Launch Sessions and the saved configuration it is a working copy of.

Nothing on the page writes back to a saved configuration on its own - a run does
not save - so the page has to say, at all times, whether what is on screen is
still the thing that was opened. That is what the red Save button is, and these
tests are what keeps it honest. Each test gets files and settings of its own, so
none of them starts from what another one left behind.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QSettings

from cms_gui import core as core_mod
from cms_gui import launch, launchcontroller as lc, launchprojectsfile as lpf, \
    launchpresetsfile as lps
from cms_gui.pages.launch import SCENARIO_MODES, UNSAVED, LaunchSessionsPage
from cms_gui.settings import Settings


def _inventory():
    return core_mod.Inventory({
        "envs": [{"alias": "claim-dev", "value": "https://claim-dev.example.com/",
                  "origin": "https://claim-dev.example.com", "count": 1},
                 {"alias": "localhost", "value": "localhost:8069",
                  "origin": "http://localhost:8069", "count": 1}],
        "users": [{"env": "https://claim-dev.example.com/", "login": "role_agent",
                   "class": "Agent", "tests": [], "has_password": True},
                  {"env": "localhost:8069", "login": "admin",
                   "class": "Admin", "tests": [], "has_password": True}],
        "scenarios": [], "extensions": [], "tags": [],
    })


def _two_ports():
    return core_mod.Inventory({
        "envs": [{"alias": "localhost:8069", "value": "localhost:8069",
                  "origin": "http://localhost:8069", "count": 1},
                 {"alias": "localhost:8070", "value": "localhost:8070",
                  "origin": "http://localhost:8070", "count": 1}],
        "users": [{"env": "localhost:8069", "login": "admin", "class": "Admin",
                   "tests": [], "has_password": True},
                  {"env": "localhost:8070", "login": "admin", "class": "Admin",
                   "tests": [], "has_password": True}],
        "scenarios": [], "extensions": [], "tags": [],
    })


class _Place:
    """The files and settings one test's pages share - a restart is a new page
    on the same place."""

    def __init__(self, root):
        self.root = root
        self.settings = Settings()
        self.settings._qs = QSettings(str(root / "gui.ini"), QSettings.IniFormat)

    def page(self, inventory=None):
        registry = lpf.Registry(str(self.root / lpf.FILE_NAME))
        controller = lc.LaunchController(self.settings, registry,
                                         str(self.root / lps.FILE_NAME),
                                         str(self.root / "launch-drafts.json"),
                                         str(self.root / "configs.json"))
        page = LaunchSessionsPage(self.settings, controller=controller)
        if inventory is not None:
            page.set_inventory(inventory)
        return page


@pytest.fixture
def place(qapp, tmp_path):
    return _Place(tmp_path)


@pytest.fixture
def page(place):
    widget = place.page(_inventory())
    yield widget
    widget.variables_panel.hide()
    widget.close()


def _pick_env(page, env_id):
    index = page.env_combo.findData(env_id)
    assert index >= 0, env_id
    page.env_combo.setCurrentIndex(index)
    page._environment_picked(index)


def _pick_config(page, name):
    index = page.config_combo.findText(name)
    assert index >= 0, name
    page.config_combo.setCurrentIndex(index)
    page._config_picked(index)


def _open_saved(page, name="Admin Access"):
    """Save what is on screen under ``name`` and open it, as Save as does."""
    preset = page.controller.save_as(page.state(), name)
    page._saved(preset)
    return preset


def _marked(page):
    return {page.footer_save.property("dirty")}


def _flag(argv, name):
    for arg in argv:
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


LOCAL = "env:localhost:8069"
DEV = "env:https://claim-dev.example.com/"


# ---------------------------------------------------------------- unsaved edits

def test_a_freshly_opened_configuration_is_not_dirty(page):
    _open_saved(page)
    assert not page.is_dirty()
    assert _marked(page) == {"false"}


def test_an_edit_marks_save_and_says_so(page):
    _open_saved(page)
    page.jobs.setValue(page.jobs.value() + 3)
    assert page.is_dirty()
    assert _marked(page) == {"true"}
    assert page.footer_save.toolTip() == 'Unsaved changes to "Admin Access"'
    assert page.unsaved_note.isVisibleTo(page)


def test_saving_settles_it_again(page):
    _open_saved(page)
    page.keep_open.setChecked(not page.keep_open.isChecked())
    assert page.is_dirty()
    page.save_configuration()
    assert not page.is_dirty()
    assert _marked(page) == {"false"}
    assert page.footer_save.toolTip() == ""


def test_going_back_to_the_saved_value_settles_it_too(page):
    """Dirty is "differs from the file", not "has been touched"."""
    _open_saved(page)
    before = page.jobs.value()
    page.jobs.setValue(before + 2)
    assert page.is_dirty()
    page.jobs.setValue(before)
    assert not page.is_dirty()


def test_a_page_nobody_has_touched_is_quiet(page):
    """Opening the app is not an edit, whatever the settings were left at."""
    assert page._current_config_name() == ""
    assert not page.is_dirty()
    assert _marked(page) == {"false"}


def test_an_edit_with_nothing_opened_is_flagged_too(page):
    """(unsaved) has no file behind it, which is the reason to flag it."""
    page.jobs.setValue(page.jobs.value() + 4)
    assert page.is_dirty()
    assert "not saved" in page.footer_save.toolTip()


# ------------------------------------------------- switching never loses an edit

def test_opening_another_configuration_is_not_an_edit(page):
    _open_saved(page, "One")
    _open_saved(page, "Two")
    _pick_config(page, "One")
    assert not page.is_dirty()
    _pick_config(page, "Two")
    assert not page.is_dirty()


def test_an_edit_stays_with_its_configuration_across_a_switch(page):
    _open_saved(page, "One")
    before = page.jobs.value()
    _open_saved(page, "Two")
    _pick_config(page, "One")
    page.jobs.setValue(7)
    _pick_config(page, "Two")
    assert page.jobs.value() == before and not page.is_dirty()
    _pick_config(page, "One")
    assert page.jobs.value() == 7, "One's edit was lost on the way back"
    assert page.is_dirty() and page.unsaved_note.isVisibleTo(page)


def test_each_environment_keeps_its_own_new_configuration(page):
    _pick_env(page, LOCAL)
    page.jobs.setValue(5)
    _pick_env(page, DEV)
    page.jobs.setValue(2)
    _pick_env(page, LOCAL)
    assert page.jobs.value() == 5
    _pick_env(page, DEV)
    assert page.jobs.value() == 2


def test_an_edit_survives_a_switch_and_a_restart(place):
    first = place.page(_inventory())
    try:
        _open_saved(first, "One")
        _open_saved(first, "Two")
        _pick_config(first, "One")
        first.jobs.setValue(11)
        _pick_config(first, "Two")
    finally:
        first.close()
    second = place.page(_inventory())
    try:
        assert second._current_config_name() == "Two"
        assert second.open_configuration("One")
        assert second.jobs.value() == 11 and second.is_dirty()
    finally:
        second.close()


def test_an_unsaved_edit_survives_the_window_closing(place):
    first = place.page(_inventory())
    try:
        _open_saved(first, "Overnight")
        first.jobs.setValue(first.jobs.value() + 5)
        assert first.is_dirty()
    finally:
        first.close()
    second = place.page()                 # before --describe answers
    try:
        assert second._current_config_name() == "Overnight"
        assert second.is_dirty()
        assert _marked(second) == {"true"}
        second.set_inventory(_inventory())
        assert second.is_dirty()
    finally:
        second.close()


def test_revert_puts_the_saved_one_back_and_forgets_the_edit(page):
    _open_saved(page, "One")
    before = page.jobs.value()
    page.jobs.setValue(before + 6)
    assert page.reset_button.text() == "Revert"
    page.reset()
    assert page.jobs.value() == before and not page.is_dirty()
    assert page.controller.draft() is None
    assert page._current_config_name() == "One"


def test_opening_from_search_never_asks_and_never_loses_the_edit(page, monkeypatch):
    from cms_gui import widgets
    monkeypatch.setattr(widgets, "confirm",
                        lambda *a, **k: pytest.fail("nothing to confirm"))
    _open_saved(page, "One")
    two = _open_saved(page, "Two")
    _pick_config(page, "One")
    page.jobs.setValue(13)
    assert page.show_found(two.id)
    assert page._current_config_name() == "Two"
    one = page.controller.presets.find("One")[0]
    assert page.show_found(one.id) and page.jobs.value() == 13


def test_search_lists_saved_configurations_and_opens_one(page):
    preset = _open_saved(page, "Shop smoke")
    _pick_config(page, UNSAVED)
    hits = page.search_rows()
    assert [h.title for h in hits] == ["Shop smoke"]
    assert hits[0].target == preset.id and hits[0].detail
    assert page.show_found(preset.id)
    assert page._current_config_name() == "Shop smoke"
    assert not page.show_found("c-gone")


# ------------------------------------------------- desktop links and history

def test_open_configuration_applies_the_saved_one_and_refuses_an_unknown_name(page):
    """What a desktop link calls: the saved settings, not whatever was on screen."""
    preset = _open_saved(page, "Link")
    saved_jobs = page.jobs.value()
    page.jobs.setValue(saved_jobs + 3)          # an unsaved edit of Link
    _pick_config(page, UNSAVED)
    page.jobs.setValue(saved_jobs + 8)          # and other settings on screen
    assert page.open_configuration(preset.id, use_draft=False)
    assert page.jobs.value() == saved_jobs and not page.is_dirty()
    assert page.open_configuration("Link", use_draft=False)     # an old link
    assert not page.open_configuration("No such configuration")


def test_a_shared_name_is_not_guessed_for_a_link(page):
    _open_saved(page, "Smoke")
    page.controller.presets.put(lps.Preset("", "Smoke", "p-other", DEV, 0,
                                           launch.merged({})))
    assert not page.open_configuration("Smoke", use_draft=False)
    assert len(page.link_candidates("Smoke")) == 2


def test_a_desktop_link_names_the_id_quoted_for_the_desktop(page, tmp_path,
                                                            monkeypatch):
    from cms_gui.pages import launch as launch_page
    preset = _open_saved(page, "Night $run")

    class Field:
        def __init__(self, value):
            self.value = value

        def text(self):
            return self.value

    class Accept:
        def __init__(self, *_args):
            self.name_edit = Field("QAVector: night")
            self.path_edit = Field(str(tmp_path))
            self.icon_edit = Field("")

        def exec(self):
            return launch_page.QDialog.Accepted
    monkeypatch.setattr(launch_page, "DesktopLinkDialog", Accept)
    page.desktop_link.setChecked(True)
    page._toggle_desktop_link()
    written = (tmp_path / "QAVector_Night_$run.desktop").read_text(encoding="utf-8")
    assert '--launch "%s"' % preset.id in written
    assert page.settings.desktop_links()[preset.id]["name"] == "QAVector: night"
    assert page._exec_arg('a "b" $c %d') == '"a \\"b\\" \\$c %%d"'


def test_renaming_keeps_the_id_and_follows_a_taken_name(page, monkeypatch):
    from cms_gui import widgets
    _open_saved(page, "Taken")
    mine = _open_saved(page, "Mine")
    page.jobs.setValue(page.jobs.value() + 5)    # an unsaved edit travels along
    monkeypatch.setattr(widgets, "ask_text", lambda *a, **k: "Taken")
    page.rename_configuration()
    assert page._current_config_name() == "Taken (2)"
    assert page.controller.preset_id == mine.id
    assert page.controller.draft() is not None and page.is_dirty()


def test_a_run_from_history_opens_as_new_and_leaves_the_saved_one_alone(page):
    preset = _open_saved(page, "Kept")
    past = dict(page.state())
    past["sessions"] = dict(past["sessions"], jobs=17)
    page.open_unsaved(past)
    assert page._current_config_name() == ""
    assert page.jobs.value() == 17
    assert page.controller.presets.get(preset.id).config == preset.config


def test_a_desktop_link_run_remembers_nothing(page):
    _open_saved(page, "Quiet")
    remembered = page.settings.launch_state()
    page.set_persistent(False)
    page.jobs.setValue(page.jobs.value() + 9)
    assert page.settings.launch_state() == remembered
    assert page.controller.draft() is None
    page.set_persistent(True)


def test_deleting_the_configuration_keeps_what_is_on_screen_as_new(page, monkeypatch):
    from cms_gui import widgets
    monkeypatch.setattr(widgets, "confirm", lambda *a, **k: True)
    preset = _open_saved(page)
    page.jobs.setValue(page.jobs.value() + 9)
    jobs = page.jobs.value()
    page.delete_configuration()
    assert page.controller.presets.get(preset.id) is None
    assert page._current_config_name() == "" and page.jobs.value() == jobs
    assert not page.is_dirty()


def test_a_save_made_elsewhere_is_offered_as_a_copy_not_written_over(page,
                                                                     monkeypatch):
    from cms_gui import widgets
    preset = _open_saved(page, "Shared")
    page.controller.presets.put(page.controller.presets.get(preset.id))  # elsewhere
    page.jobs.setValue(page.jobs.value() + 1)
    asked = []
    monkeypatch.setattr(widgets, "choose",
                        lambda *a, **k: asked.append(a) or "Save as a copy")
    page.save_configuration()
    assert asked
    assert page._current_config_name() == "Shared (2)"
    assert page.controller.presets.get(preset.id).revision == 2


# ------------------------------------------------- environments

def test_changing_an_environment_from_here_is_announced(page):
    seen = []
    page.environments_changed.connect(lambda: seen.append(True))
    _pick_env(page, LOCAL)
    page._variable_defined("port", "8069", "environment")
    assert seen and page.controller.environment().variables == {"port": "8069"}
    assert not page.env_move.isVisibleTo(page)


def test_environments_come_from_the_controller_and_configurations_follow(page):
    assert [page.env_combo.itemData(i) for i in range(page.env_combo.count())] == \
        [DEV, LOCAL]
    _open_saved(page, "Here")
    _pick_env(page, DEV if page.controller.environment_id == LOCAL else LOCAL)
    assert page.config_combo.findText("Here") < 0, "listed under another environment"


def test_the_chosen_environment_comes_back_after_a_restart(place):
    first = place.page(_inventory())
    try:
        _pick_env(first, DEV)
        _open_saved(first, "Admin")
    finally:
        first.close()
    second = place.page(_inventory())
    try:
        assert second.controller.environment_id == DEV
        assert second.state()["environment_value"] == "https://claim-dev.example.com/"
        assert not second.is_dirty()
    finally:
        second.close()


def test_a_configuration_whose_environment_is_gone_is_kept_and_flagged(place):
    (place.root / "configs.json").write_text(json.dumps(
        {"Old local": launch.merged({"environment": "localhost:8071"})}))
    page = place.page(_two_ports())
    try:
        envs = [page.env_combo.itemText(i) for i in range(page.env_combo.count())]
        assert "localhost:8071 (missing)" in envs
        assert page.open_configuration("Old local")
        assert page.controller.environment_id == "missing:localhost:8071"
        assert any("localhost:8071" in p for p in page.problem_list())
        assert "users.json" in page.env_note.text()
        assert not page.run_button.isEnabled()
        assert page.env_move.isVisibleTo(page), "the way out has to be on screen"
    finally:
        page.close()


def test_an_old_alias_names_what_it_could_have_meant_and_can_be_moved(place,
                                                                       monkeypatch):
    from cms_gui import widgets
    (place.root / "configs.json").write_text(json.dumps(
        {"Older": launch.merged({"environment": "localhost"})}))
    page = place.page(_two_ports())
    try:
        assert page.open_configuration("Older")
        assert any("localhost:8069, localhost:8070" in p for p in page.problem_list())
        monkeypatch.setattr(widgets, "ask_item", lambda *a, **k: "localhost:8070")
        page.use_with_another_environment()
        assert page.controller.environment_id == "env:localhost:8070"
        assert page._current_config_name() == "Older"
        assert not page.problem_list()
    finally:
        page.close()


def test_logs_that_belong_to_another_environment_are_called_out(page):
    """The state behind an empty report, said before the run rather than after."""
    page.set_inventory(core_mod.Inventory({
        "envs": [{"alias": "localhost", "value": "localhost:8069"},
                 {"alias": "dev", "value": "https://dev.example.com/"}],
        "users": [], "scenarios": [], "extensions": [],
        "log_sources": [{"name": "app", "env": "https://dev.example.com/",
                         "connection": "dev", "type": "file",
                         "target": "file (ssh) /x", "default": True}],
        "log_sources_path": "/tmp/logsources.json"}))
    _pick_env(page, LOCAL)
    page._populate_server_logs(["app"])          # as a saved configuration would
    assert "nothing will be streamed" in page.logs_note.text()


def test_a_usable_log_leaves_the_note_silent(page):
    page.set_inventory(core_mod.Inventory({
        "envs": [{"alias": "localhost", "value": "localhost:8069"}],
        "users": [], "scenarios": [], "extensions": [],
        "log_sources": [{"name": "app", "env": "localhost:8069", "connection": "here",
                         "type": "file", "target": "file (local) /x",
                         "default": True}],
        "log_sources_path": "/tmp/logsources.json"}))
    _pick_env(page, LOCAL)
    page._populate_server_logs([])
    assert page.logs_note.text() == ""


def test_no_logs_configured_at_all_says_where_to_configure_them(page):
    page.set_inventory(core_mod.Inventory({
        "envs": [{"alias": "localhost", "value": "localhost:8069"}],
        "users": [], "scenarios": [], "extensions": [], "log_sources": [],
        "log_sources_path": "/tmp/logsources.json"}))
    assert "No server logs configured" in page.logs_note.text()
    assert "Log sources page" in page.logs_note.text()


# ------------------------------------------------- where the windows open

def test_the_start_address_is_under_the_environment_not_in_advanced(page):
    assert not hasattr(page, "url")
    assert not page.advanced.isAncestorOf(page.url_field)


def test_the_box_shows_the_environment_s_address_and_says_so(page):
    _pick_env(page, LOCAL)
    assert page.url_field.text() == "http://localhost:8069"
    assert page.address_source == "environment"
    assert not page.url_reset.isVisibleTo(page)
    assert _flag(page.argv(), "--url") is None
    assert _flag(page.argv(), "--env") == "localhost:8069"


def test_an_address_of_its_own_is_this_run_s_until_it_is_saved(page):
    _pick_env(page, LOCAL)
    _open_saved(page, "Own address")
    page._url_edited("localhost:8074")
    assert page.state()["advanced"]["url"] == "localhost:8074"
    assert page.url_field.text() == "http://localhost:8074"
    assert page.address_source == "this run"
    assert page.url_reset.isVisibleTo(page)
    assert _flag(page.argv(), "--url") == "http://localhost:8074"
    page.save_configuration()
    assert page.address_source == "this configuration"
    page.url_reset.click()
    assert page.state()["advanced"]["url"] == ""
    assert page.url_field.text() == "http://localhost:8069"


def test_committing_the_address_that_applies_anyway_is_not_an_override(page):
    _pick_env(page, LOCAL)
    page._url_edited("http://localhost:8069")
    assert page.state()["advanced"]["url"] == ""
    assert page.address_source == "environment"


def test_a_bad_address_stops_run_before_any_window_opens(page):
    _pick_env(page, LOCAL)
    page._url_edited("localhost:99999")
    assert any("port" in p for p in page.problem_list())
    assert not page.run_button.isEnabled()
    assert page.url_problem.isVisibleTo(page) and "port" in page.url_problem.text()
    page._url_edited("")
    assert not page.url_problem.isVisibleTo(page)


def test_the_environment_s_own_address_is_what_opens(page):
    _pick_env(page, LOCAL)
    page.controller.set_environment_address("localhost:8090")
    page.refresh_address()
    assert page.url_field.text() == "http://localhost:8090"
    assert page.address_source == "environment address"
    assert _flag(page.argv(), "--url") == "http://localhost:8090"


def test_default_folders_are_shown_not_copied_in(page):
    page.settings.set_directory("reports", "/srv/reports")
    page.set_inventory(_inventory())
    edit = page.dir_edits["reports_dir"]
    assert edit.text() == "" and edit.placeholderText() == "/srv/reports"
    assert page.state()["advanced"]["reports_dir"] == ""


# ------------------------------------------------- variables in the address

def _with_port(page):
    _pick_env(page, LOCAL)
    page.controller.set_environment_value("port", "8069")
    page.controller.set_environment_address("http://localhost:{{port}}/web")
    page.refresh_address()


def _rows(page):
    """The rows the variables panel was last given."""
    captured = []
    original = page.variables_panel.set_rows
    page.variables_panel.set_rows = lambda rows, **k: captured.append(rows)
    try:
        page._fill_variables()
    finally:
        page.variables_panel.set_rows = original
    return {row["name"]: row for row in captured[-1]}


def test_a_variable_s_value_is_blue_in_the_address_and_the_rest_is_not(page):
    _with_port(page)
    assert page.url_field.text() == "http://localhost:8069/web"
    assert ("8069", "port") in page.url_field._parts
    assert page.url_field.label.text().count("<a ") == 1


def test_a_value_set_for_this_run_changes_where_it_opens(page):
    _with_port(page)
    page._variable_changed("port", "8074")
    assert page.state()["variables"] == {"port": "8074"}
    assert page.url_field.text() == "http://localhost:8074/web"
    assert _flag(page.argv(), "--url") == "http://localhost:8074/web"
    assert _flag(page.argv(), "--env") == "localhost:8069"     # same profiles
    row = _rows(page)["port"]
    assert row["source"] == "this run"
    assert (row["inherited"], row["inherited_source"]) == ("8069", "environment")
    page._variable_changed("port", None)
    assert page.state()["variables"] == {}
    assert page.url_field.text() == "http://localhost:8069/web"


def test_clicking_the_blue_value_opens_the_panel_on_it(page):
    _with_port(page)
    page.url_field.value_clicked.emit("port")
    assert page.variables_panel.isVisible()
    assert page.variables_panel.editor("port") is not None
    page.variables_panel.hide()


def test_save_keeps_the_value_with_the_configuration(page):
    _with_port(page)
    page._variable_changed("port", "8074")
    preset = _open_saved(page, "Port 8074")
    assert page.controller.presets.get(preset.id).variables == {"port": "8074"}
    assert _rows(page)["port"]["source"] == "configuration"


def test_set_as_environment_default_moves_the_value_down(page):
    _with_port(page)
    page._variable_changed("port", "8074")
    page._variable_for_environment("port", "8074")
    assert page.state()["variables"] == {}
    assert page.controller.environment().variables == {"port": "8074"}
    assert page.url_field.text() == "http://localhost:8074/web"


def test_an_unknown_variable_is_defined_from_the_page(page):
    _pick_env(page, LOCAL)
    page._url_edited("localhost:{{port}}")
    assert page.url_define.isVisibleTo(page) and "port" in page.url_define.text()
    assert any("{{port}} has no value" in p for p in page.problem_list())
    page._variable_defined("port", "8075", "run")
    assert not page.url_define.isVisibleTo(page)
    assert page.url_field.text() == "http://localhost:8075"
    assert page.state()["variables"] == {"port": "8075"}


def test_a_value_the_address_cannot_take_stops_the_run(page):
    """Values are text: what a port has to be is the address's business."""
    _with_port(page)
    page._variable_changed("port", "99999")
    assert page.problem_list()
    assert page.url_problem.isVisibleTo(page)


# ------------------------------------------------- the rest of the page

def test_auto_and_all_at_once_are_not_both_on(page):
    page.jobs_all.setChecked(True)
    page.jobs_auto.setChecked(True)
    assert not page.jobs_all.isChecked()
    page.jobs_all.setChecked(True)
    assert not page.jobs_auto.isChecked()


def test_the_number_is_greyed_out_when_it_is_not_deciding(page):
    assert page.jobs.isEnabled()
    page.jobs_auto.setChecked(True)
    assert not page.jobs.isEnabled()
    page.jobs_auto.setChecked(False)
    assert page.jobs.isEnabled()


def test_the_sessions_button_says_what_is_in_force(page):
    page.jobs.setValue(1)
    assert page.sessions_button.text() == "One at a time"
    page.jobs.setValue(3)
    assert page.sessions_button.text() == "3 at a time"
    page.jobs_all.setChecked(True)
    assert page.sessions_button.text() == "All at once"
    page.jobs_auto.setChecked(True)
    assert page.sessions_button.text() == "Auto"


def test_the_number_and_the_load_live_in_the_panel_not_the_page(page):
    panel = page.sessions_panel
    assert not panel.isVisible()
    for widget in (page.jobs, page.jobs_all, page.jobs_auto, page.metrics_warning):
        assert panel.isAncestorOf(widget)
    page.sessions_button.click()
    assert panel.isVisible()
    panel.hide()


def test_memory_pressure_is_said_on_the_page_too(page, monkeypatch):
    from cms_gui import load as load_mod
    strained = load_mod.Load(cpu_percent=50.0, available_kb=5, total_kb=100)
    monkeypatch.setattr(page._load, "read", lambda: strained)
    page.what.set_current("Run scenarios")
    page._update_metrics()
    assert page.load_warning.isVisibleTo(page)
    assert "pressure" in page.load_warning.text()


def test_server_logs_have_a_fold_of_their_own_and_extensions_sit_in_advanced(page):
    assert page.logs_fold.isAncestorOf(page.logs_mode)
    assert page.advanced.isAncestorOf(page.report_mode)
    assert page.advanced.isAncestorOf(page.lock_windows)
    assert page.advanced.isAncestorOf(page.ext_mode)
    assert page.advanced.isAncestorOf(page.ext_list)


def test_auto_login_is_on_by_default_and_never_a_choice_of_the_list(page):
    assert page.advanced.isAncestorOf(page.autologin)
    assert page.autologin.isChecked() and page.autologin.isEnabled()
    assert "--no-autologin" not in page.argv()
    page.ext_mode.set_current("Choose")
    page.ext_list.set_all(True)
    assert "_autologin" not in page.state()["extensions"]["names"]


def test_auto_login_turned_off_is_kept_with_the_configuration(page):
    page.autologin.setChecked(False)
    assert page.state()["extensions"]["autologin"] is False
    assert "--no-autologin" in page.argv()
    _open_saved(page, "Signs in by itself")
    page.autologin.setChecked(True)
    assert page.is_dirty()
    page.reset()
    assert not page.autologin.isChecked()


def test_auto_survives_a_save_and_reload(page):
    page.jobs_auto.setChecked(True)
    page.keep_open.setChecked(False)
    assert page.state()["sessions"]["auto_jobs"] is True


def test_the_summary_folds_away_but_the_buttons_never_do(page):
    page.summary_toggle.setChecked(True)
    assert page.summary.isVisibleTo(page)
    page.summary_toggle.setChecked(False)
    assert not page.summary.isVisibleTo(page)
    assert not page.notes.isVisibleTo(page)
    assert not page.preview.isVisibleTo(page)
    assert page.run_button.isVisibleTo(page)


def test_folding_survives_the_next_edit(page):
    page.summary_toggle.setChecked(False)
    page.jobs.setValue(page.jobs.value() + 1)
    assert not page.summary.isVisibleTo(page)


def test_a_problem_is_never_folded_away(page):
    page.summary_toggle.setChecked(False)
    page.users_mode.set_current("Choose accounts", notify=False)
    page.users_list.set_all(False)
    page._changed()
    assert page.problems.text()
    assert page.problems.isVisibleTo(page)


def test_the_fold_is_remembered(page):
    page.summary_toggle.setChecked(False)
    assert page.settings.launch_summary_expanded is False
    page.summary_toggle.setChecked(True)
    assert page.settings.launch_summary_expanded is True


def test_the_overflow_menu_holds_the_rare_things(page):
    assert page.more_button.text() == ""
    assert not page.more_button.icon().isNull()
    assert [a.text() for a in page.more_menu.actions() if not a.isSeparator()] == [
        "Rename…", "Use with another environment…", "Desktop Link", "Delete…"]
    assert page.desktop_link.isCheckable()
    assert page.more_button.property("menuglyph") == "true"


def test_the_overflow_entries_need_a_saved_configuration(page):
    assert not page.desktop_link.isEnabled()
    assert not page.use_elsewhere.isEnabled()
    _open_saved(page, "Nightly")
    assert page.desktop_link.isEnabled() and page.use_elsewhere.isEnabled()


def test_an_edit_takes_the_link_entry_away_again(page):
    _open_saved(page, "Nightly")
    page.jobs.setValue(page.jobs.value() + 1)
    assert not page.desktop_link.isEnabled()


def test_fire_and_forget_turns_the_server_logs_block_off(page):
    page.detach.setChecked(True)
    assert not page.logs_mode.isEnabled()
    assert "leave the windows running" in page.logs_note.text()
    page.detach.setChecked(False)
    assert page.logs_mode.isEnabled()
    assert "leave the windows running" not in page.logs_note.text()


def test_the_guide_and_window_switches_are_saved_and_restored(page):
    page.report_doc.setChecked(True)
    page.maximized.setChecked(True)
    page.lock_windows.setChecked(True)
    state = page.state()
    page.report_doc.setChecked(False)
    page.maximized.setChecked(False)
    page.lock_windows.setChecked(False)
    page.set_state(state)
    assert page.report_doc.isChecked() and page.maximized.isChecked()
    assert page.lock_windows.isChecked()


def test_ticking_full_screen_is_an_edit(page):
    _open_saved(page)
    page.maximized.setChecked(True)
    assert _marked(page) == {"true"}


def _accounts_inventory():
    return core_mod.Inventory({
        "envs": [{"alias": "local", "value": "localhost:8069", "count": 2}],
        "users": [{"env": "localhost:8069", "login": "admin",
                   "tests": ["shop_order", "tag:smoke"]},
                  {"env": "localhost:8069", "login": "agent",
                   "tests": ["shop_refund"]}],
        "scenarios": [{"id": "shop_order"}, {"id": "shop_refund"},
                      {"id": "shop_cancel"}],
        "extensions": [], "tags": ["smoke"],
    })


def _per_user(page):
    for index, (mode, _label, _hint) in enumerate(SCENARIO_MODES):
        if mode == launch.SCENARIOS_PER_USER:
            page.scenario_mode.button(index).setChecked(True)


def test_each_account_s_own_list_shows_the_chosen_accounts_scenarios(page):
    page.set_inventory(_accounts_inventory())
    _per_user(page)
    assert set(page.scenario_list.checked()) == {"shop_order", "tag:smoke",
                                                 "shop_refund"}
    assert page.scenario_list.selected_only()
    assert not page.scenario_list.isEnabled()
    page.users_mode.set_current("Choose accounts")
    page.users_list.set_checked(["agent"], notify=True)
    assert page.scenario_list.checked() == ["shop_refund"]


def test_showing_the_accounts_lists_leaves_the_pick_alone(page):
    page.set_inventory(_accounts_inventory())
    page.scenario_mode.button(3).setChecked(True)            # Choose scenarios
    page.scenario_list.set_checked(["shop_cancel"], notify=True)
    _per_user(page)
    assert page.state()["scenarios"]["selected"] == ["shop_cancel"]
    page.scenario_mode.button(3).setChecked(True)
    assert page.scenario_list.checked() == ["shop_cancel"]
    assert not page.scenario_list.selected_only()


# ------------------------------------------------- what to do

def test_open_windows_shows_only_what_opening_uses(page):
    page.what.set_current("Open windows")
    assert page.state()["scenarios"]["mode"] == launch.SCENARIOS_NONE
    assert not page.scenario_list.isVisibleTo(page)
    assert not page.reports_box.isEnabled()
    assert not page.reports_note.isHidden()
    assert not page.keep_open.isVisibleTo(page)
    assert page.what_hint.isVisibleTo(page)
    assert page.sessions_box.isVisibleTo(page)


def test_changing_what_to_do_never_changes_the_page_height(page):
    page.resize(1100, 700)
    page.show()
    heights = set()
    for what in ("Open windows", "Run scenarios", "Record", "Open windows"):
        page.what.set_current(what)
        page.layout().activate()
        page.scrollArea.widget().layout().activate()
        heights.add(page.scrollArea.widget().sizeHint().height())
    page.hide()
    assert len(heights) == 1


def test_accounts_and_what_to_do_are_one_fixed_height(page):
    accounts = page.users_list.parentWidget()
    what = page.what_scroll.parentWidget()
    assert accounts.minimumHeight() == accounts.maximumHeight() \
        == what.minimumHeight() == what.maximumHeight()
    assert what.isAncestorOf(page.scenario_list)
    assert page.what_scroll.widget().isAncestorOf(page.sessions_box)


def test_run_scenarios_comes_back_to_the_scenarios_it_had(page):
    page.what.set_current("Run scenarios")
    page.scenario_mode.button(3).setChecked(True)            # Choose scenarios
    page._picked = ["shop_order"]
    page.scenario_list.set_checked(["shop_order"])
    page.what.set_current("Open windows")
    state = page.state()
    assert state["scenarios"]["mode"] == launch.SCENARIOS_NONE
    assert state["scenarios"]["run_mode"] == launch.SCENARIOS_PICK
    assert state["scenarios"]["selected"] == ["shop_order"]
    page.what.set_current("Run scenarios")
    assert page.state()["scenarios"]["mode"] == launch.SCENARIOS_PICK
    assert page.reports_box.isEnabled()
    assert page.reports_note.isHidden()


def test_record_is_kept_with_the_configuration_and_runs_nothing(page):
    page.what.set_current("Record")
    assert page.recording() and page.state()["record"] is True
    assert page.state()["scenarios"]["mode"] == launch.SCENARIOS_NONE
    assert not page.sessions_box.isVisibleTo(page)
    _open_saved(page, "Recording")
    page.what.set_current("Open windows")
    assert page.is_dirty()
    page.reset()                                   # Revert: what was saved
    assert page.what.current() == "Record" and page.recording()


def test_a_scenario_mode_picked_directly_is_run_scenarios(page):
    page.what.set_current("Open windows")
    page.scenario_mode.button(1).setChecked(True)            # every scenario
    assert page.what.current() == "Run scenarios"


# ------------------------------------------------- projects

def _with_project(page):
    """A Helpdesk project holding an environment of its own."""
    registry = page.controller.registry

    def change(projects):
        project = lpf.ProjectRow("p-help", "Helpdesk", [
            lpf.EnvironmentRow("e-help", "Local helpdesk", "localhost:8069",
                               "http://localhost:8070")])
        projects.append(project)
    registry.update(change)
    page.set_inventory(_inventory())


def test_the_project_decides_which_environments_are_offered(page):
    _with_project(page)
    projects = [page.project_combo.itemData(i) for i in range(page.project_combo.count())]
    assert projects == ["p-help", lpf.WORKSPACE]
    index = page.project_combo.findData("p-help")
    page.project_combo.setCurrentIndex(index)
    page._project_picked(index)
    envs = [page.env_combo.itemData(i) for i in range(page.env_combo.count())]
    assert envs == ["e-help"]
    assert page.url_field.text() == "http://localhost:8070"
    back = page.project_combo.findData(lpf.WORKSPACE)
    page.project_combo.setCurrentIndex(back)
    page._project_picked(back)
    assert page.controller.environment_id in (LOCAL, DEV)
    assert "e-help" not in [page.env_combo.itemData(i)
                            for i in range(page.env_combo.count())]


def test_the_footer_says_where_this_run_is(page):
    _pick_env(page, LOCAL)
    text = page.context_line.text()
    assert text.startswith("Workspace / localhost") and "localhost:8069" in text
    assert "1 account" in text


# ------------------------------------------------- + Account

def test_an_account_added_here_is_chosen_once_the_inventory_has_it(page, monkeypatch):
    from cms_gui.pages import launchaccountform
    _pick_env(page, LOCAL)
    page.users_mode.set_current("Choose accounts")
    page.users_list.set_checked(["admin"], notify=True)

    class Saved:
        def __init__(self, *_args):
            self.saved_login = "new@example.invalid"

        def exec(self):
            from PySide6.QtWidgets import QDialog
            return QDialog.Accepted
    monkeypatch.setattr(launchaccountform, "AccountForm", Saved)
    announced = []
    page.accounts_changed.connect(lambda: announced.append(True))
    page._add_account()
    assert announced
    grown = _inventory()
    grown.payload["users"].append({"env": "localhost:8069", "login": "new@example.invalid",
                                   "class": "Operator", "tests": [],
                                   "has_password": True})
    page.set_inventory(grown)
    assert set(page.state()["users"]["logins"]) == {"admin", "new@example.invalid"}


def test_an_account_needs_a_real_environment(page, monkeypatch):
    from cms_gui import widgets
    said = []
    monkeypatch.setattr(widgets, "note", lambda *a, **k: said.append(a))
    page.controller.environment_id = lc.ALL_ENVIRONMENTS
    page._add_account()
    assert said


def test_the_address_box_is_told_which_variables_it_can_finish(page):
    _with_port(page)
    assert page.url_field.editor._names == ["port"]


def test_the_more_button_stands_level_with_the_combos(page, qapp):
    page.show()
    qapp.processEvents()
    combo = page.config_combo
    centre = combo.mapTo(page, combo.rect().center()).y()
    button = page.more_button
    assert abs(button.mapTo(page, button.rect().center()).y() - centre) <= 1


def test_the_address_box_is_told_which_variables_it_can_finish(page):
    _with_port(page)
    assert page.url_field.editor._names == ["port"]


# ------------------------------------------------- recording into a scenario

def _recordable_inventory():
    inventory = _inventory()
    inventory.payload["scenarios"] = [
        {"id": "shop_order", "name": "Order", "writable": True},
        {"id": "bundled_demo", "name": "Demo", "writable": False}]
    return inventory


def test_record_offers_a_new_scenario_or_one_of_your_own(page):
    page.set_inventory(_recordable_inventory())
    page.what.set_current("Record")
    assert page.record_box.isVisibleTo(page)
    targets = [page.record_into.itemData(i) for i in range(page.record_into.count())]
    assert targets == ["", "shop_order"], "a bundled scenario cannot be recorded into"
    page.record_into.setCurrentIndex(1)
    assert page.state()["record_into"] == "shop_order" and page.record_target() == "shop_order"
    page.what.set_current("Run scenarios")
    assert not page.record_box.isVisibleTo(page)


def test_a_scenario_to_record_into_that_is_gone_is_kept_and_said(page):
    state = dict(page.state(), record=True, record_into="vanished")
    page.set_state(state)
    page.set_inventory(_recordable_inventory())
    assert page.record_into.currentText() == "vanished (not found)"
    assert any("vanished" in p for p in page.problem_list())


def test_dont_offer_to_save_passwords_is_kept_with_the_configuration(page):
    assert not page.advanced.isAncestorOf(page.no_password_prompts) is False
    page.no_password_prompts.setChecked(True)
    assert page.state()["sessions"]["no_password_prompts"] is True
    assert "--no-password-prompts" in page.argv()
    _open_saved(page, "Quiet passwords")
    page.no_password_prompts.setChecked(False)
    page.reset()
    assert page.no_password_prompts.isChecked()
