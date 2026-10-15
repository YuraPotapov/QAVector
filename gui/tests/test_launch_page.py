"""Launch Sessions and the saved configuration it is a working copy of.

Nothing on the page writes back to a saved configuration on its own - a run does
not save, and opening another one drops the edit - so the page has to say, at all
times, whether what is on screen is still the thing that was opened. That is what
the red Save button is, and these tests are what keeps it honest.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cms_gui import theme
from cms_gui import launch
from cms_gui.pages.launch import SCENARIO_MODES, UNSAVED, LaunchSessionsPage
from cms_gui.settings import Settings


@pytest.fixture
def page(qapp):
    page = LaunchSessionsPage(Settings())
    yield page
    for name in list(page._configs.names()):
        page._configs.remove(name)
    page.settings.launch_config_name = ""
    page.close()


def _open_saved(page, name="Admin Access"):
    """Save what is on screen under ``name`` and select it, as the buttons do."""
    page._configs.put(name, page.state())
    page.settings.launch_config_name = name
    page._reload_configs(select=name)
    page._mark_clean()
    return name


def _marked(page):
    return {button.property("dirty") for button in (page.config_save,
                                                    page.footer_save)}


def test_a_freshly_opened_configuration_is_not_dirty(page):
    _open_saved(page)
    assert not page.is_dirty()
    assert _marked(page) == {"false"}


def test_an_edit_marks_both_save_buttons(page):
    _open_saved(page)
    page.jobs.setValue(page.jobs.value() + 3)
    assert page.is_dirty()
    assert _marked(page) == {"true"}
    assert page.config_save.toolTip() == 'Unsaved changes to "Admin Access"'


def test_saving_settles_it_again(page):
    _open_saved(page)
    page.keep_open.setChecked(not page.keep_open.isChecked())
    assert page.is_dirty()
    page.save_configuration()
    assert not page.is_dirty()
    assert _marked(page) == {"false"}
    assert page.config_save.toolTip() == ""


def test_going_back_to_the_saved_value_settles_it_too(page):
    """Dirty is "differs from the file", not "has been touched"."""
    _open_saved(page)
    before = page.jobs.value()
    page.jobs.setValue(before + 2)
    assert page.is_dirty()
    page.jobs.setValue(before)
    assert not page.is_dirty()


def test_opening_another_configuration_is_not_an_edit(page):
    _open_saved(page, "One")
    page.jobs.setValue(7)
    _open_saved(page, "Two")
    assert not page.is_dirty()
    page.config_combo.setCurrentText("One")
    assert not page.is_dirty()
    assert page.jobs.value() != 7          # One's own value came back


def test_a_page_nobody_has_touched_is_quiet(page):
    """Opening the app is not an edit, whatever the settings were left at."""
    assert page._current_config_name() == ""
    assert not page.is_dirty()
    assert _marked(page) == {"false"}


def test_an_edit_with_nothing_opened_is_flagged_too(page):
    """(unsaved) has no file behind it, which is the reason to flag it.

    These settings are stored under no name at all, so an edit to them is the
    most unsaved thing on the page. Save is what settles it - there it asks for
    a name first.
    """
    page.config_combo.setCurrentText(UNSAVED)
    page._mark_clean()
    page.jobs.setValue(page.jobs.value() + 4)
    assert page.is_dirty()
    assert _marked(page) == {"true"}
    assert "not saved" in page.config_save.toolTip()


def test_deleting_the_configuration_starts_the_settings_over(page):
    """Delete keeps what is on screen, so that becomes the new starting point."""
    name = _open_saved(page)
    page.jobs.setValue(9)
    assert page.is_dirty()
    page._configs.remove(name)             # the dialog's outcome, without the dialog
    page.settings.launch_config_name = ""
    page._reload_configs(select=UNSAVED)
    page._mark_clean()
    assert not page.is_dirty()
    page.jobs.setValue(2)
    assert page.is_dirty()


def test_an_unsaved_edit_survives_the_window_closing(qapp):
    """The working state is restored on the next start - so is the warning.

    Reopening the page puts back what was on screen, not what was in the file.
    Coming back to a Save button that had gone quiet again is how an edit gets
    lost without anyone noticing.
    """
    settings = Settings()
    first = LaunchSessionsPage(settings)
    try:
        _open_saved(first, "Overnight")
        first.jobs.setValue(first.jobs.value() + 5)
        assert first.is_dirty()
    finally:
        first.close()

    second = LaunchSessionsPage(Settings())
    try:
        assert second._current_config_name() == "Overnight"
        assert second.is_dirty()
        assert _marked(second) == {"true"}
    finally:
        for name in list(second._configs.names()):
            second._configs.remove(name)
        second.settings.launch_config_name = ""
        second.close()


# ----------------------------------------------------- the environment survives

def _inventory():
    from cms_gui import core as core_mod

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


def test_the_chosen_environment_comes_back_after_a_restart(qapp):
    """The environment is the one control whose choices arrive late.

    Every list on this page is filled from --describe, which lands ~100ms after
    the window is built - so the page is restored against an environment combo
    that still holds nothing but "All environments". Setting a non-editable
    QComboBox to an item it does not have is a silent no-op, and the restored
    choice is gone before the inventory ever turns up.
    """
    settings = Settings()
    first = LaunchSessionsPage(settings)
    try:
        first.set_inventory(_inventory())
        first.env_combo.setCurrentText("claim-dev")
        _open_saved(first, "Admin")
        assert first.state()["environment"] == "claim-dev"
        assert not first.is_dirty()
    finally:
        first.close()

    second = LaunchSessionsPage(Settings())
    try:
        # Restored before --describe answers, exactly as the window does it.
        assert second.state()["environment"] == "claim-dev", \
            "the restored environment was dropped on the way in"
        second.set_inventory(_inventory())
        assert second.state()["environment"] == "claim-dev", \
            "the inventory arriving reset the environment"
        assert not second.is_dirty(), \
            "nothing was touched, so no Save button should be red"
    finally:
        for name in list(second._configs.names()):
            second._configs.remove(name)
        second.settings.launch_config_name = ""
        second.close()


def test_an_environment_the_config_no_longer_has_is_kept_and_flagged(qapp):
    """Removing an env from users.json must not silently rewrite a saved run.

    The accounts, extensions and scenario lists all keep a value the inventory
    has stopped offering; the environment now does too. Keeping it means the note
    beside it has to explain itself, or it reads as an ordinary choice that would
    launch nothing.
    """
    page = LaunchSessionsPage(Settings())
    try:
        page.set_inventory(_inventory())
        page.env_combo.setCurrentText("claim-dev")
        assert page.state()["environment"] == "claim-dev"

        from cms_gui import core as core_mod
        page.set_inventory(core_mod.Inventory({"envs": [], "users": []}))

        assert page.state()["environment"] == "claim-dev"
        assert "users.json" in page.env_note.text()
    finally:
        page.settings.launch_config_name = ""
        page.close()


def test_auto_and_all_at_once_are_not_both_on(page):
    page.jobs_all.setChecked(True)
    page.jobs_auto.setChecked(True)
    # They answer the same question two ways; the last one asked wins rather than
    # leaving the page holding a contradiction it would have to resolve silently.
    assert not page.jobs_all.isChecked()
    page.jobs_all.setChecked(True)
    assert not page.jobs_auto.isChecked()
    page.jobs_all.setChecked(False)     # the page persists these; leave it as found


def test_the_number_is_greyed_out_when_it_is_not_deciding(page):
    page.jobs_all.setChecked(False)     # the page restores the last run's answers
    page.jobs_auto.setChecked(False)
    assert page.jobs.isEnabled()
    page.jobs_auto.setChecked(True)
    # Left editable it would read as a value in force, which it is not.
    assert not page.jobs.isEnabled()
    page.jobs_auto.setChecked(False)
    assert page.jobs.isEnabled()


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
    # RUN is always one click away, wherever the footer is folded to.
    assert page.run_button.isVisibleTo(page)


def test_folding_survives_the_next_edit(page):
    # _changed() settles these labels on every edit; if the fold were a second
    # writer, the next keystroke would quietly unfold what was just folded.
    page.summary_toggle.setChecked(False)
    page.jobs.setValue(page.jobs.value() + 1)
    assert not page.summary.isVisibleTo(page)


def test_a_problem_is_never_folded_away(page):
    # It is the reason RUN would refuse; hiding the explanation is the worst of both.
    page.summary_toggle.setChecked(False)
    page.jobs_auto.setChecked(True)
    page.keep_open.setChecked(True)
    page.users_mode.set_current("Choose accounts", notify=False)
    page.users_list.set_all(False)
    page._changed()
    assert page.problems.text()                     # something is wrong ...
    assert page.problems.isVisibleTo(page)          # ... and it is on screen


def test_the_fold_is_remembered(page):
    page.summary_toggle.setChecked(False)
    assert page.settings.launch_summary_expanded is False
    page.summary_toggle.setChecked(True)
    assert page.settings.launch_summary_expanded is True


def test_desktop_link_moved_into_the_overflow_menu(page):
    # Rare, per-configuration housekeeping: one unlabelled button, not a third
    # thing competing with Save and RUN for the same glance.
    assert page.more_button.text() == ""
    assert not page.more_button.icon().isNull()
    assert [a.text() for a in page.more_menu.actions()] == ["Desktop Link"]
    assert page.desktop_link.isCheckable()
    # The icon IS the menu hint, so Qt must not draw its own arrow under it.
    assert page.more_button.property("menuglyph") == "true"


def test_the_overflow_entry_needs_a_saved_configuration(page):
    page.config_combo.setCurrentText(UNSAVED)
    page._mark_clean()
    page._update_dirty()
    # A desktop link points at a configuration by name; there has to be one.
    assert not page.desktop_link.isEnabled()
    _open_saved(page, "Nightly")
    assert page.desktop_link.isEnabled()


def test_an_edit_takes_the_overflow_entry_away_again(page):
    _open_saved(page, "Nightly")
    assert page.desktop_link.isEnabled()
    page.jobs.setValue(page.jobs.value() + 1)
    # What is on screen no longer matches the file the link would point at.
    assert not page.desktop_link.isEnabled()


def test_fire_and_forget_turns_the_server_logs_block_off(page):
    page.detach.setChecked(True)
    assert not page.logs_mode.isEnabled()
    assert "leave the windows running" in page.logs_note.text()


def test_unticking_it_puts_the_block_back(page):
    page.detach.setChecked(True)
    page.detach.setChecked(False)
    assert page.logs_mode.isEnabled()
    # And does not leave its own explanation behind.
    assert "leave the windows running" not in page.logs_note.text()


def _log_inventory(log_sources):
    from cms_gui import core as core_mod
    return core_mod.Inventory({
        "envs": [{"alias": "localhost", "value": "localhost:8069"},
                 {"alias": "dev", "value": "https://dev.example.com/"}],
        "users": [], "scenarios": [], "extensions": [],
        "log_sources": log_sources, "log_sources_path": "/tmp/logsources.json"})


def test_no_logs_configured_at_all_says_where_to_configure_them(page):
    page.set_inventory(_log_inventory([]))
    assert "No server logs configured" in page.logs_note.text()
    assert "Log sources page" in page.logs_note.text()


def test_logs_that_belong_to_another_environment_are_called_out(page):
    """The state behind an empty report, said before the run rather than after.

    A saved configuration keeps the log names it was saved with; switching the
    environment leaves them pointing at a stand this run does not touch, and the
    launcher then streams nothing.
    """
    page.set_inventory(_log_inventory([{"name": "app", "env": "https://dev.example.com/",
                                    "connection": "dev", "type": "file",
                                    "target": "file (ssh) /x", "default": True}]))
    page._populate_server_logs(["app"])          # as a saved configuration would
    page._select_env("localhost")
    page._populate_server_logs(["app"])
    assert "nothing will be streamed" in page.logs_note.text()


def test_a_usable_log_leaves_the_note_silent(page):
    page.set_inventory(_log_inventory([{"name": "app", "env": "localhost:8069",
                                    "connection": "here", "type": "file",
                                    "target": "file (local) /x", "default": True}]))
    page._select_env("localhost")
    page._populate_server_logs([])
    assert page.logs_note.text() == ""


def test_the_guide_checkbox_is_saved_and_restored(page):
    assert not page.report_doc.isChecked()
    page.report_doc.setChecked(True)
    state = page.state()
    assert state["reports"]["doc"] is True
    page.report_doc.setChecked(False)
    page.set_state(state)
    assert page.report_doc.isChecked()


# ------------------------------------------------- each account's own scenarios

def _accounts_inventory():
    from cms_gui import core as core_mod
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
    page.reset()                       # earlier tests leave their environment
    page.set_inventory(_accounts_inventory())
    _per_user(page)
    assert set(page.scenario_list.checked()) == {"shop_order", "tag:smoke",
                                                 "shop_refund"}
    assert page.scenario_list.selected_only()
    assert not page.scenario_list.isEnabled()

    page.users_mode.set_current("Choose accounts")
    page.users_list.set_checked(["agent"], notify=True)
    assert page.scenario_list.checked() == ["shop_refund"]
    page.users_list.set_checked([], notify=True)
    assert page.scenario_list.checked() == []
    page.reset()                                    # the page persists; leave it


def test_showing_the_accounts_lists_leaves_the_pick_alone(page):
    page.reset()
    page.set_inventory(_accounts_inventory())
    page.scenario_mode.button(3).setChecked(True)            # Choose scenarios
    page.scenario_list.set_checked(["shop_cancel"], notify=True)
    _per_user(page)
    assert page.state()["scenarios"]["selected"] == ["shop_cancel"]
    page.scenario_mode.button(3).setChecked(True)
    assert page.scenario_list.checked() == ["shop_cancel"]
    assert not page.scenario_list.selected_only()   # the view it had comes back
    page.reset()                                    # the page persists; leave it
