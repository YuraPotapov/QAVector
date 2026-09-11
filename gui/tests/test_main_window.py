"""The two interaction levels, and the switch between them.

Launch Sessions is the interface for a user and Command is the one for a
developer, so the window has to answer two questions correctly at all times:
which of them is reachable, and which of them RUN acts on. It also has to put a
recorded run back where it came from - and the Command page's share of that is
``set_state``, which it already had: these tests are what proves the low-level
page needed no changes to gain a history.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cms_gui import history as history_mod, launch, main_window as main_window_mod
from cms_gui.settings import Settings


@pytest.fixture
def window(qapp, dispose):
    # What the rail is showing persists, so without this one test's View menu is
    # where the next test starts from.
    settings = Settings()
    settings.sidebar_collapsed = False
    settings.save_hidden_nav_items([])
    win = main_window_mod.MainWindow()
    win.set_developer_mode(False)
    win.show()
    qapp.processEvents()
    yield win
    win.history.clear()
    dispose(win)


def _visible(window, key):
    """Whether a nav entry is offered (the rail itself is shown)."""
    return not window._nav_buttons[key].isHidden()


def _menu_labels(window, title):
    """Every entry in one of the menu bar's menus, ampersands stripped."""
    for action in window.menuBar().actions():
        if action.text().replace("&", "") == title:
            return [entry.text().replace("&", "")
                    for entry in action.menu().actions() if entry.text()]
    raise AssertionError("no %s menu" % title)


def _divider_breaks(window, qapp):
    """Colours found down the rail's rightmost pixel that are not the divider."""
    from cms_gui import theme

    window.resize(1200, 800)
    window.show()
    qapp.processEvents()
    qapp.processEvents()

    rail = window._nav_buttons["launch"].parentWidget()
    image = rail.grab().toImage()
    edge = rail.width() - 1
    wrong = {}
    for y in range(image.height()):
        name = image.pixelColor(edge, y).name().lower()
        if name != theme.DIVIDER.lower():
            wrong[name] = wrong.get(name, 0) + 1
    return wrong


# ------------------------------------------------------------------ navigation

def test_the_navigation_has_both_groups_in_the_intended_order(window):
    assert [key for key, _l, _g in main_window_mod.CONFIGURE] == [
        "environments", "credentials", "logsources", "scenarios", "commands",
        "launch"]
    assert [key for key, _l, _g in main_window_mod.OBSERVE] == [
        "run", "log", "artifacts", "history"]


def test_the_rail_is_wide_enough_for_every_label_it_holds(window):
    """The rail used to be a fixed 206px, which clipped once labels grew.

    "Launch Sessions" and a letter-spaced "CONFIGURE" both exceed it under a wider
    body font - a condensed family missing, a larger system font, display scaling -
    and a fixed-width QFrame clips instead of growing. So the width is measured,
    and this asserts the measurement covers the widest thing in there.
    """
    from cms_gui import widgets

    rail = window._nav_buttons["launch"].parentWidget()
    usable = rail.width() - 1          # the frame's own border-right
    for key, button in window._nav_buttons.items():
        assert button.sizeHint().width() <= usable, key

    for title in ("Configure", "Observe"):
        heading = widgets.kicker(title)
        heading.setContentsMargins(main_window_mod.RAIL_PADDING, 8,
                                   main_window_mod.RAIL_PADDING, 6)
        # Less the trailing letter-spacing, which is measured but never drawn.
        ink = heading.sizeHint().width() - main_window_mod.HEADING_TRIM
        assert ink <= rail.width(), title


def test_the_rail_never_shrinks_below_its_floor(window):
    rail = window._nav_buttons["launch"].parentWidget()
    assert rail.width() >= main_window_mod.RAIL_MIN_WIDTH


def test_the_sidebar_divider_runs_unbroken_down_the_whole_rail(window, qapp):
    """Nothing in the rail may paint over the frame's own border.

    Regression, two causes at once: the global `QWidget { background }` rule gave
    every QLabel an opaque fill, so the CONFIGURE and OBSERVE headings punched
    holes in the divider, and the active nav item's accent fill punched another.
    A stylesheet border is painted inside the widget rect and children are not
    clipped to the contents rect, so full-width children have to leave it a pixel.
    """
    breaks = _divider_breaks(window, qapp)
    assert not breaks, "the divider is broken by %s" % breaks


def test_the_divider_survives_the_rail_being_collapsed(window, qapp):
    """The same border, and a rail with different things in it.

    Collapsing changes every width in there - the frame's, each button's, the
    handle's - and the one pixel on the right is reserved by a margin rather
    than by anything that measures.
    """
    window.set_sidebar_collapsed(True)
    breaks = _divider_breaks(window, qapp)
    assert not breaks, "the divider is broken by %s" % breaks


def test_a_group_heading_sits_on_the_sidebar_tint_not_the_page_colour(window, qapp):
    from cms_gui import theme

    window.resize(1200, 800)
    window.show()
    qapp.processEvents()
    qapp.processEvents()

    rail = window._nav_buttons["launch"].parentWidget()
    image = rail.grab().toImage()
    # A few pixels left of the "CONFIGURE" text, inside its row.
    counts = {}
    for y in range(14, 30):
        for x in range(4, 12):
            name = image.pixelColor(x, y).name().lower()
            counts[name] = counts.get(name, 0) + 1
    dominant = max(counts.items(), key=lambda kv: kv[1])[0]
    assert dominant == theme.NEUTRAL[100].lower()


def test_the_window_has_the_application_icon(window):
    assert not window.windowIcon().isNull()


# ------------------------------------------------------ collapsing the rail
# 196px of labels is a lot to give a navigation that is read once and then known.
# Collapsed the rail keeps the marks and gives the width back to the page.

def test_collapsing_keeps_the_marks_and_says_what_each_one_is(window):
    """With no label drawn, the label has to be reachable some other way.

    A column of unexplained marks is a guessing game; the same marks with a
    tooltip each is what every sidebar that collapses has settled on.
    """
    from PySide6.QtCore import Qt

    window.set_sidebar_collapsed(True)
    for key, button in window._nav_buttons.items():
        assert button.toolButtonStyle() == Qt.ToolButtonIconOnly, key
        assert button.toolTip() == window._nav_labels[key], key
        assert not button.icon().isNull(), key

    window.set_sidebar_collapsed(False)
    for key, button in window._nav_buttons.items():
        assert button.toolButtonStyle() == Qt.ToolButtonTextBesideIcon, key
        # Beside a label that is right there, a tooltip is a wait for something
        # already on screen.
        assert button.toolTip() == "", key


def test_the_collapsed_rail_is_narrower_and_still_fits_its_marks(window, qapp):
    """Measured, not assumed: an icon-only button's width is the style's doing."""
    rail = window._nav_buttons["launch"].parentWidget()
    expanded = rail.width()
    window.set_sidebar_collapsed(True)
    qapp.processEvents()

    assert rail.width() < expanded
    usable = rail.width() - 1          # the frame's own border-right
    for key, button in window._nav_buttons.items():
        assert button.sizeHint().width() <= usable, key


def test_the_group_headings_go_where_they_cannot_be_read(window):
    """"CONFIGURE" does not fit in a rail of marks, and a clipped word is worse
    than no word."""
    window.set_sidebar_collapsed(True)
    assert all(heading.isHidden() for heading, _keys in window._nav_headings)
    window.set_sidebar_collapsed(False)
    assert not any(heading.isHidden() for heading, _keys in window._nav_headings)


def test_the_menu_entry_and_the_rails_own_handle_stay_in_step(window):
    window.collapse_action.setChecked(True)
    assert window.settings.sidebar_collapsed is True
    assert "Expand" in window.collapse_button.toolTip()
    window.set_sidebar_collapsed(False)
    assert window.collapse_action.isChecked() is False
    assert "Collapse" in window.collapse_button.toolTip()


def test_the_collapsed_rail_survives_a_restart(window, dispose):
    window.set_sidebar_collapsed(True)
    again = main_window_mod.MainWindow()
    try:
        assert again.settings.sidebar_collapsed is True
        assert again._nav_buttons["launch"].toolTip() == "Launch Sessions"
    finally:
        again.set_sidebar_collapsed(False)
        dispose(again)


# ------------------------------------------------ choosing what the rail holds
# Ten entries, and most people use three. Each one can be switched off in View,
# which is stored as what is *hidden* so a page added in a later version arrives
# on the rail rather than having to be found and switched on.

def test_a_page_can_be_taken_off_the_rail(window):
    window.set_nav_item_visible("artifacts", False)
    assert not _visible(window, "artifacts")
    assert window._nav_actions["artifacts"].isChecked() is False
    assert "artifacts" in window.settings.hidden_nav_items()

    window.set_nav_item_visible("artifacts", True)
    assert _visible(window, "artifacts")
    assert "artifacts" not in window.settings.hidden_nav_items()


def test_taking_the_page_you_are_on_off_the_rail_moves_you_off_it(window):
    """Otherwise the window sits on a page with no way back to any other."""
    window.show_page("history")
    assert window.stack.currentWidget() is window.history_page
    window.set_nav_item_visible("history", False)
    assert window.stack.currentWidget() is not window.history_page


def test_a_heading_goes_with_the_last_item_under_it(window):
    """A heading over an empty space names an empty space."""
    for key, _label, _mark in main_window_mod.OBSERVE:
        window.set_nav_item_visible(key, False)
    headings = dict((tuple(keys), heading)
                    for heading, keys in window._nav_headings)
    observe = headings[tuple(k for k, _l, _m in main_window_mod.OBSERVE)]
    configure = headings[tuple(k for k, _l, _m in main_window_mod.CONFIGURE)]
    assert observe.isHidden()
    assert not configure.isHidden()


def test_the_last_item_on_the_rail_cannot_be_taken_off(window):
    """A rail with nothing in it is a window with no way off the page it is on."""
    for key, _label, _mark in main_window_mod.CONFIGURE + main_window_mod.OBSERVE:
        if key != "launch":
            window.set_nav_item_visible(key, False)

    window.set_nav_item_visible("launch", False)
    assert _visible(window, "launch")
    # And the menu says so, rather than showing a tick that did not take.
    assert window._nav_actions["launch"].isChecked() is True


def test_the_per_item_switch_and_developer_mode_do_not_undo_each_other(window):
    """Two switches decide whether Command shows, and both have to agree."""
    window.set_developer_mode(True)
    assert _visible(window, "commands")
    window.set_nav_item_visible("commands", False)
    assert not _visible(window, "commands")

    window.set_developer_mode(False)
    window.set_developer_mode(True)
    assert not _visible(window, "commands")
    window.set_nav_item_visible("commands", True)
    assert _visible(window, "commands")


def test_leaving_developer_mode_gives_the_rail_back_rather_than_emptying_it(window):
    """Everything switched off but Command, and then Command goes away too."""
    window.set_developer_mode(True)
    for key, _label, _mark in main_window_mod.CONFIGURE + main_window_mod.OBSERVE:
        if key != "commands":
            window.set_nav_item_visible(key, False)

    window.set_developer_mode(False)
    assert _visible(window, "launch")


def test_what_the_rail_holds_survives_a_restart(window, dispose):
    window.set_nav_item_visible("log", False)
    again = main_window_mod.MainWindow()
    try:
        assert not _visible(again, "log")
        assert again._nav_actions["log"].isChecked() is False
    finally:
        again.settings.save_hidden_nav_items([])
        dispose(again)


def test_launch_sessions_is_offered_in_both_modes(window):
    assert _visible(window, "launch")
    window.set_developer_mode(True)
    assert _visible(window, "launch")


def test_history_is_offered_in_both_modes(window):
    assert _visible(window, "history")
    window.set_developer_mode(True)
    assert _visible(window, "history")


def test_command_is_only_offered_in_developer_mode(window):
    assert not _visible(window, "commands")
    window.set_developer_mode(True)
    assert _visible(window, "commands")
    window.set_developer_mode(False)
    assert not _visible(window, "commands")


def test_the_menus_name_things_plainly_until_developer_mode(window):
    """A flag is the answer to a question a regular user is not asking.

    Developer mode already decides which pages are reachable; the wording is the
    same decision. "Refresh --describe" told a person launching sessions nothing
    they could act on, and told the one building a command line exactly what they
    needed - so it is written both ways and the mode picks.
    """
    window.set_developer_mode(False)
    plain = _menu_labels(window, "Tools")
    assert not [label for label in plain if "--" in label], plain
    # Still says what each one does.
    assert any("help" in label.lower() for label in plain), plain

    window.set_developer_mode(True)
    spelled_out = _menu_labels(window, "Tools")
    assert any("--describe" in label for label in spelled_out), spelled_out
    assert any("--help" in label for label in spelled_out), spelled_out


def test_the_wording_reaches_the_pages_not_just_the_menus(window):
    """The window does not keep a list of which pages care - it asks each one."""
    from PySide6.QtWidgets import QLabel

    def text_of(page):
        return "\n".join(l.text() for l in page.findChildren(QLabel))

    window.set_developer_mode(True)
    assert "--flows-dir" in text_of(window.environments)
    window.set_developer_mode(False)
    assert "--" not in text_of(window.environments)


def test_the_cli_facing_toolbar_button_follows_the_mode(window):
    # "Copy command" has nothing to offer someone who never sees a command.
    assert window.copy_button.isHidden()
    window.set_developer_mode(True)
    assert not window.copy_button.isHidden()


def test_the_mode_button_is_out_of_sight_but_still_follows_the_mode(window):
    # The mode lives under View -> Developer mode now. The button is kept, and
    # kept in step, so it never says something stale if it comes back.
    assert window.developer_button.isHidden()
    assert "off" in window.developer_button.text()
    assert not window.developer_button.isChecked()
    window.set_developer_mode(True)
    assert "on" in window.developer_button.text()
    assert window.developer_button.isChecked()
    assert window.developer_button.property("variant") == "primary"
    assert window.developer_button.isHidden()


def test_the_menu_item_and_the_button_stay_in_step(window):
    window.developer_action.setChecked(True)
    assert window.developer_button.isChecked()
    assert window.settings.developer_mode
    window.developer_button.setChecked(False)
    assert not window.developer_action.isChecked()
    assert not window.settings.developer_mode


def test_the_mode_survives_a_restart(window, qapp, dispose):
    window.set_developer_mode(True)
    again = main_window_mod.MainWindow()
    try:
        assert again.settings.developer_mode
        assert _visible(again, "commands")
    finally:
        again.set_developer_mode(False)
        dispose(again)


def test_leaving_developer_mode_while_on_the_command_page_moves_you_off_it(window):
    window.set_developer_mode(True)
    window.show_page("commands")
    assert window.stack.currentWidget() is window.command
    window.set_developer_mode(False)
    assert window.stack.currentWidget() is window.launch


def test_the_command_page_cannot_be_reached_in_regular_mode(window):
    window.show_page("commands")
    assert window.stack.currentWidget() is window.launch


def test_an_unknown_page_key_lands_on_launch_sessions(window):
    window.show_page("no-such-page")
    assert window.stack.currentWidget() is window.launch


# ------------------------------------------------------------------ which page runs

def test_run_acts_on_launch_sessions_in_regular_mode(window):
    assert window._run_page()[0] == "launch"
    assert window._run_page()[1] is window.launch


def test_run_follows_the_last_launching_page_you_visited(window):
    window.set_developer_mode(True)
    window.show_page("commands")
    assert window._run_source == "commands"
    assert window._run_page()[1] is window.command
    window.show_page("launch")
    assert window._run_source == "launch"
    assert window._run_page()[1] is window.launch


def test_visiting_an_observing_page_does_not_change_what_run_does(window):
    window.show_page("launch")
    window.show_page("log")
    assert window._run_source == "launch"


def test_command_stops_being_the_run_target_when_developer_mode_goes_off(window):
    window.set_developer_mode(True)
    window.show_page("commands")
    window.set_developer_mode(False)
    # Nothing can run a page the user cannot see.
    assert window._run_page()[1] is window.launch


# ------------------------------------------------------------------ history

def test_restoring_a_command_entry_reproduces_the_form_exactly(window):
    """The Command page contributes ``set_state`` and nothing else.

    Restoring is the one thing history asks of the low-level page, and it was
    already part of its API - which is why the page itself is untouched by all of
    this.
    """
    state = dict(window.command.state())
    state.update({"--env": "localhost:8069", "--run-tests": "all",
                  "--jobs": "3", "--detach": True})
    entry = {"kind": history_mod.COMMAND, "command_state": state}

    assert window.restore_entry(entry) == "commands"
    assert window.stack.currentWidget() is window.command
    restored = window.command.state()
    for name in ("--env", "--run-tests", "--jobs", "--detach"):
        assert restored[name] == state[name], name


def test_restoring_a_command_entry_turns_developer_mode_on(window):
    assert not window.settings.developer_mode
    window.restore_entry({"kind": history_mod.COMMAND,
                          "command_state": {"--env": "localhost:8069"}})
    # Otherwise it would restore into a page the user has no way to look at.
    assert window.settings.developer_mode
    assert window.stack.currentWidget() is window.command


def test_restoring_a_launch_entry_reproduces_the_configuration(window):
    config = launch.merged({
        "environment": "",
        "users": {"mode": launch.USERS_PICK, "logins": ["admin"]},
        "scenarios": {"mode": launch.SCENARIOS_PICK, "selected": ["auth.login"]},
        "sessions": {"jobs": 4, "all_at_once": False, "keep_open": False,
                     "detach": False},
        "screenshots": {"mode": launch.SHOTS_FINISH}})

    assert window.restore_entry({"kind": history_mod.LAUNCH,
                                 "launch_config": config}) == "launch"
    assert window.stack.currentWidget() is window.launch
    restored = window.launch.state()
    assert restored["users"] == {"mode": launch.USERS_PICK, "logins": ["admin"]}
    assert restored["scenarios"]["selected"] == ["auth.login"]
    assert restored["sessions"]["jobs"] == 4
    assert restored["sessions"]["keep_open"] is False
    assert restored["screenshots"]["mode"] == launch.SHOTS_FINISH
    # And it means the same thing it meant when it ran.
    assert window.launch.argv() == launch.argv(config, window.inventory)


def test_a_restored_launch_entry_keeps_a_selection_the_core_no_longer_offers(window):
    """A scenario that has been deleted must not silently vanish from a config.

    Dropping it would turn "run these three" into "run these two" the moment the
    entry was opened, with nothing said about it.
    """
    config = launch.merged({"scenarios": {"mode": launch.SCENARIOS_PICK,
                                          "selected": ["ghost.scenario"]}})
    window.restore_entry({"kind": history_mod.LAUNCH, "launch_config": config})
    assert window.launch.state()["scenarios"]["selected"] == ["ghost.scenario"]


def test_a_run_is_recorded_before_it_starts_and_closed_when_it_ends(window):
    entry_id = window.history.begin(history_mod.LAUNCH,
                                   {"summary": "localhost:8069 · admin"})
    window._entry_id = entry_id
    window._stopping = False
    window._close_entry(history_mod.status_for(0), exit_code=0)

    entry = window.history.entry(entry_id)
    assert entry["status"] == history_mod.OK
    assert entry["exit_code"] == 0
    assert entry["finished_at"]
    # The log is archived at the end of the run, because the next run clears it.
    assert entry["log_file"] and os.path.isfile(entry["log_file"])


def test_stopping_a_run_is_recorded_as_stopped_not_failed(window):
    entry_id = window.history.begin(history_mod.LAUNCH, {})
    window._entry_id = entry_id
    window._stopping = True
    window._close_entry(history_mod.status_for(130, stopped=True), exit_code=130)
    assert window.history.entry(entry_id)["status"] == history_mod.STOPPED


def test_closing_an_entry_twice_does_nothing_the_second_time(window):
    entry_id = window.history.begin(history_mod.LAUNCH, {})
    window._entry_id = entry_id
    window._close_entry(history_mod.OK, exit_code=0)
    window._close_entry(history_mod.FAILED, exit_code=1)
    assert window.history.entry(entry_id)["status"] == history_mod.OK


def test_the_history_page_shows_runs_from_both_pages(window, qapp):
    window.history.begin(history_mod.LAUNCH,
                         {"launch_config": {"environment": "localhost:8069"}})
    window.history.begin(history_mod.COMMAND,
                         {"command_state": {"--env": "staging", "--run-tests": "all"}})
    qapp.processEvents()
    page = window.history_page
    assert page.table.rowCount() == 2

    # Filtered by where it ran, not by which page started it - both kinds answer.
    combo = page.environment_filter
    combo.setCurrentIndex(combo.findData("staging"))
    assert page.table.rowCount() == 1
    assert page.table.item(0, 2).text() == "staging"
    assert page.table.item(0, 4).text() == "all"

    combo.setCurrentIndex(combo.findData("localhost:8069"))
    assert page.table.rowCount() == 1
    assert "localhost:8069" in page.table.item(0, 2).text()

    combo.setCurrentIndex(0)                    # All environments
    assert page.table.rowCount() == 2


def test_the_history_page_describes_a_launch_entry_without_naming_a_flag(window):
    from cms_gui.pages import history as history_page

    entry = {"kind": history_mod.LAUNCH, "launch_config": launch.merged(
        {"environment": "localhost:8069",
         "scenarios": {"mode": launch.SCENARIOS_ALL, "selected": []}})}
    text = " ".join("%s %s" % row for row in history_page.detail_rows(entry))
    assert "--" not in text


def test_the_history_page_describes_a_command_entry_with_the_flags_it_used(window):
    from cms_gui.pages import history as history_page

    entry = {"kind": history_mod.COMMAND,
             "command_state": {"--env": "localhost:8069", "--detach": True,
                               "--log-level": "INFO", "--url": ""}}
    rows = dict(history_page.detail_rows(entry))
    assert rows["--env"] == "localhost:8069"
    assert rows["--detach"] == "on"
    # A flag left at its default, or empty, was not part of the command.
    assert "--log-level" not in rows
    assert "--url" not in rows


# ------------------------------------------------- continuing a recording
# Recording usually means adding to something half-written, so RUN's menu has to
# offer that - and has to name the file it would write to, since appending to
# somebody's scenario is not a thing to do by accident.

def _inventory_with(window, *rows):
    from cms_gui import core as core_mod

    inventory = core_mod.Inventory({"scenarios": list(rows), "blocks": [],
                                    "selectors": {}, "envs": [], "users": []})
    window.inventory = inventory
    window.scenarios.set_inventory(inventory)
    return inventory


def _scenario_row(flow_id, writable=True):
    return {"id": flow_id, "name": flow_id, "tags": [], "writable": writable,
            "source": "user" if writable else "bundled", "in_all": True}


def test_with_nothing_selected_a_recording_is_new_and_asks_nothing(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "exec",
                        lambda *a, **k: pytest.fail("asked when there was nothing to ask"))
    _inventory_with(window, _scenario_row("mine"))
    assert window.recordable_scenario() == ""
    assert window.ask_recording_target() == ("new", "")


def test_the_scenario_open_in_the_editor_is_what_continues(window):
    _inventory_with(window, _scenario_row("mine"))
    window.scenarios.current = {"id": "mine", "writable": True}
    assert window.recordable_scenario() == "mine"


def test_a_bundled_scenario_is_never_offered(window):
    # It cannot be written back, so recording into it would collect steps and
    # then throw them away.
    _inventory_with(window, _scenario_row("shipped", writable=False))
    window.scenarios.current = {"id": "shipped", "writable": False}
    assert window.recordable_scenario() == ""


def test_exactly_one_scenario_chosen_on_launch_sessions_counts(window):
    _inventory_with(window, _scenario_row("picked"), _scenario_row("other"))
    window.scenarios.current = None
    window.launch.set_state({"scenarios": {"mode": "pick", "selected": ["picked"]}})
    assert window.recordable_scenario() == "picked"


def test_two_chosen_scenarios_are_ambiguous_so_neither_is_used(window):
    _inventory_with(window, _scenario_row("a"), _scenario_row("b"))
    window.scenarios.current = None
    window.launch.set_state({"scenarios": {"mode": "pick", "selected": ["a", "b"]}})
    assert window.recordable_scenario() == ""


def test_a_selected_scenario_is_confirmed_before_being_added_to(window, monkeypatch):
    """Appending to a scenario and replacing one look identical until too late."""
    from PySide6.QtWidgets import QMessageBox

    _inventory_with(window, _scenario_row("mine"))
    window.scenarios.current = {"id": "mine", "writable": True}

    seen = {}

    def answer(box):
        seen["text"] = box.text()
        # The default is the safe one: adding to what is there. Clicking the
        # button itself is what sets clickedButton(), which is what is read.
        chosen = box.defaultButton()
        seen["default"] = chosen.text()
        chosen.click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", answer)
    assert window.ask_recording_target() == ("continue", "mine")
    assert "mine" in seen["text"]
    assert "mine" in seen["default"]


def test_start_new_is_offered_even_when_something_is_selected(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    _inventory_with(window, _scenario_row("mine"))
    window.scenarios.current = {"id": "mine", "writable": True}

    def choose_new(box):
        for button in box.buttons():
            if button.text() == "Start new":
                button.click()
                return 0
        pytest.fail("no way to start a new scenario")

    monkeypatch.setattr(QMessageBox, "exec", choose_new)
    assert window.ask_recording_target() == ("new", "")


def test_cancelling_records_nothing(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    _inventory_with(window, _scenario_row("mine"))
    window.scenarios.current = {"id": "mine", "writable": True}

    def cancel(box):
        box.button(QMessageBox.Cancel).click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", cancel)
    assert window.ask_recording_target()[0] == "cancel"

    started = []
    monkeypatch.setattr(window, "start_run", lambda **kw: started.append(kw))
    window.start_recording()
    assert started == []


def test_the_stop_menu_offers_each_running_window(window):
    window.run_state.handle({"kind": "window.launched", "session": "dev-agent", "pid": 1})
    window.run_state.handle({"kind": "window.launched", "session": "dev-manager", "pid": 2})
    window._fill_stop_menu()
    labels = [a.text() for a in window.stop_menu.actions() if a.isEnabled()]
    assert any(l.startswith("dev-agent") for l in labels)
    assert any(l.startswith("dev-manager") for l in labels)
    assert "Stop everything" in labels


def test_a_finished_window_is_not_offered_for_stopping(window):
    window.run_state.handle({"kind": "window.launched", "session": "done", "pid": 1})
    window.run_state.sessions["done"]["state"] = "passed"
    window._fill_stop_menu()
    labels = [a.text() for a in window.stop_menu.actions()]
    assert not any(l.startswith("done") for l in labels)
    assert "No windows running" in labels


def test_stopping_one_window_with_no_launcher_is_a_no_op(window):
    window.run_state.handle({"kind": "window.launched", "session": "a", "pid": 1})
    window.stop_session("a")     # nothing is running; must not raise
    assert window.run_state.sessions["a"]["state"] == "launched"


def test_recording_one_account_asks_nothing(window):
    window.show_page("launch")
    window.launch.inventory = _StubInventory(["only_one"])
    window.launch.users_mode.set_current("Choose accounts", notify=False)
    window.launch.users_list.set_checked(["only_one"])
    # One account is not a choice, so no dialog: "" means "leave as configured".
    assert window.ask_recording_account() == ""


class _StubInventory:
    def __init__(self, logins):
        self._logins = logins

    def logins(self, env=None):
        return list(self._logins)

    def env_value(self, alias):
        return alias


def test_a_chosen_account_replaces_the_configurations_own_filter(window, monkeypatch):
    # The account list is what made the recording ambiguous, so the answer has to
    # replace it rather than be added alongside it - the core takes one.
    sent = {}
    monkeypatch.setattr(window.process, "start",
                        lambda argv, working_dir=None: sent.update(argv=argv) or True)
    monkeypatch.setattr(window.core, "is_configured", lambda: True)
    monkeypatch.setattr(window.core, "argv", lambda *a: list(a))
    monkeypatch.setattr(window.launch, "argv",
                        lambda: ["--filter-users=a,b,c", "--events=-"])
    monkeypatch.setattr(window.launch, "problem_list", lambda: [])
    window._run_source = "launch"
    window.start_run(source="launch", recorder=True, only_login="b")
    argv = sent.get("argv", [])
    assert "--filter-users=b" in argv
    assert "--filter-users=a,b,c" not in argv
    assert "--recorder" in argv


# ------------------------------------------------------ RUN ▾ -> In Background

def test_run_offers_to_run_in_the_background(window):
    assert [a.text() for a in window.run_menu.actions()] == ["In Background",
                                                             "With Recorder"]


def test_in_background_is_greyed_out_for_a_scenario_that_needs_a_browser(window,
                                                                        monkeypatch):
    window._run_source = "launch"
    monkeypatch.setattr(window.launch, "background_blockers",
                        lambda: [("simple_fine", "click, fill")])
    window.run_menu.aboutToShow.emit()          # decided as the menu opens
    assert not window.background_action.isEnabled()
    assert "simple_fine: click, fill" in window.background_action.toolTip()


def test_in_background_is_offered_for_service_steps_only(window, monkeypatch):
    window._run_source = "launch"
    monkeypatch.setattr(window.launch, "background_blockers", lambda: [])
    window.run_menu.aboutToShow.emit()
    assert window.background_action.isEnabled()


def test_the_launch_pages_own_choice_decides_it(window):
    from cms_gui import core as core_mod

    window.launch.inventory = core_mod.Inventory({
        "scenarios": [{"id": "restart", "tags": [], "in_all": True,
                       "browser_actions": []},
                      {"id": "clicks", "tags": [], "in_all": True,
                       "browser_actions": ["click"]}],
        "users": [], "envs": [], "blocks": [], "selectors": {}})
    window._run_source = "launch"
    try:
        window.launch.set_state({"scenarios": {"mode": "pick", "selected": ["restart"]}})
        assert window._update_background_action() == []
        window.launch.set_state({"scenarios": {"mode": "pick",
                                               "selected": ["restart", "clicks"]}})
        assert window._update_background_action() == [("clicks", "click")]
    finally:
        window.launch.set_state({})             # the page persists what it shows


def test_in_background_runs_the_same_command_with_no_browser(window, monkeypatch):
    sent = {}
    monkeypatch.setattr(window.process, "start",
                        lambda argv, working_dir=None: sent.update(argv=argv) or True)
    monkeypatch.setattr(window.core, "is_configured", lambda: True)
    monkeypatch.setattr(window.core, "argv", lambda *a: list(a))
    monkeypatch.setattr(window.launch, "argv",
                        lambda: ["--run-tests=restart", "--jobs=auto", "--detach",
                                 "--events=-"])
    monkeypatch.setattr(window.launch, "problem_list", lambda: [])
    monkeypatch.setattr(window.launch, "background_blockers", lambda: [])
    window._run_source = "launch"
    window.start_background_run()
    argv = sent.get("argv", [])
    assert argv[0] == "--no-browser"
    # The sessions as configured: --jobs goes through untouched.
    assert "--jobs=auto" in argv and "--detach" not in argv
    assert "in the background" in window.run.meta.text()


def test_a_stale_menu_cannot_start_a_run_the_launcher_would_refuse(window,
                                                                  monkeypatch):
    started = []
    monkeypatch.setattr(window, "start_run", lambda **kw: started.append(kw))
    monkeypatch.setattr(window.launch, "background_blockers",
                        lambda: [("clicks", "click")])
    window._run_source = "launch"
    window.start_background_run()
    assert started == []


def test_a_background_run_is_run_again_in_the_background(window, monkeypatch):
    started = []
    monkeypatch.setattr(window, "start_run",
                        lambda source=None, **kw: started.append((source, kw)))
    entry = {"kind": history_mod.LAUNCH, "launch_config": launch.merged({}),
             "argv": ["python3", "session_launcher.py", "--no-browser",
                      "--run-tests=restart"]}
    window.rerun_entry(entry)
    assert started == [("launch", {"background": True})]
    started.clear()
    entry["argv"] = ["python3", "session_launcher.py", "--run-tests=restart"]
    window.rerun_entry(entry)
    assert started == [("launch", {"background": False})]


def test_closing_with_no_run_asks_nothing(window, monkeypatch):
    asked = []
    monkeypatch.setattr(window, "_confirm_close_during_run",
                        lambda: asked.append(True) or True)
    monkeypatch.setattr(window.process, "is_running", lambda: False)
    window.close()
    assert asked == []          # nothing is at risk, so nothing to warn about


def test_closing_during_a_run_is_refused_when_cancelled(window, monkeypatch):
    stopped = []
    monkeypatch.setattr(window.process, "is_running", lambda: True)
    monkeypatch.setattr(window.process, "stop", lambda: stopped.append(True))
    monkeypatch.setattr(window, "_confirm_close_during_run", lambda: False)
    from PySide6.QtGui import QCloseEvent
    event = QCloseEvent()
    window.closeEvent(event)
    assert not event.isAccepted()      # the window stays open
    assert stopped == []               # and the run is left alone


def test_closing_during_a_run_stops_it_first(window, monkeypatch):
    order = []
    monkeypatch.setattr(window.process, "is_running", lambda: True)
    monkeypatch.setattr(window.process, "stop", lambda: order.append("stop"))
    monkeypatch.setattr(window.process, "_proc",
                        type("P", (), {"waitForFinished":
                                       lambda self, ms: order.append("wait %d" % ms)})())
    monkeypatch.setattr(window, "_confirm_close_during_run", lambda: True)
    from PySide6.QtGui import QCloseEvent
    window.closeEvent(QCloseEvent())
    # Stopped, then waited on: closing while the launcher is still shutting the
    # windows down is what loses a login.
    assert order == ["stop", "wait %d" % main_window_mod.CLOSE_WAIT_MS]


def test_the_close_warning_counts_the_windows_that_will_go():
    assert "closes 2 windows" in main_window_mod._close_warning(2)
    assert "closes 1 window" in main_window_mod._close_warning(1)
    # No windows reported yet: say what closing does without inventing a count.
    assert "closes" not in main_window_mod._close_warning(0)


def test_a_toolbar_button_with_a_menu_reserves_room_for_its_arrow(window):
    # The arrow half is drawn OVER the button, not beside it. Stop became a
    # QToolButton with a menu and kept the plain padding, so its label sat
    # underneath the arrow.
    for button in (window.run_button, window.stop_button):
        assert button.property("hasmenu") == "true"
        assert button.menu() is not None


class _FakeSplash:
    def __init__(self):
        self.finished_for = None

    def finish(self, window):
        self.finished_for = window

    def showMessage(self, *a, **k):
        pass

    def repaint(self):
        pass


def test_the_splash_never_outlives_a_core_that_never_answers(window):
    # --describe is a subprocess that can hang. Without a hard stop the splash
    # would sit there with no window behind it, which looks like a dead app.
    splash = _FakeSplash()
    window.splash = splash
    window._finish_splash()
    assert splash.finished_for is window
    assert window.splash is None


def test_finishing_the_splash_twice_is_harmless(window):
    # Three things race to close it - the load finishing, the load failing, and
    # the timeout. Whichever wins, the others must be no-ops.
    window.splash = _FakeSplash()
    window._finish_splash()
    window._finish_splash()
    assert window.splash is None


def test_the_ready_pause_is_long_enough_to_read():
    # 300ms was a flicker: the word appeared and the window took over.
    assert main_window_mod.SPLASH_READY_MS >= 500
    assert main_window_mod.SPLASH_MAX_MS > main_window_mod.SPLASH_READY_MS


def test_the_splash_artwork_is_found_in_assets():
    from cms_gui import app as app_mod

    path = app_mod._splash_file()
    assert path and os.path.isfile(path)
    assert os.path.basename(path) in app_mod.SPLASH_NAMES


def test_replacing_the_splash_is_a_file_copy_not_a_code_change(tmp_path, monkeypatch):
    # Any of the accepted names works, tried in order, so dropping a new image
    # into assets/ is all it takes.
    from cms_gui import app as app_mod

    monkeypatch.setattr(app_mod.os.path, "dirname", lambda _p: str(tmp_path))
    (tmp_path / "assets").mkdir()
    assert app_mod._splash_file() is None          # none present yet
    (tmp_path / "assets" / "splash.png").write_bytes(b"x")
    assert os.path.basename(app_mod._splash_file()) == "splash.png"
    (tmp_path / "assets" / "splash.jpg").write_bytes(b"x")
    assert os.path.basename(app_mod._splash_file()) == "splash.jpg"   # first wins


def test_always_on_top_does_not_show_the_window_early(qapp):
    """Restoring the setting must not put a half-built window on screen.

    set_always_on_top has to re-show the window because changing a flag makes Qt
    re-create it - but at startup it runs while the splash is still up and the
    pages are empty, so the window appeared first and the data arrived a second
    or two later.
    """
    settings = Settings()
    previous = settings.always_on_top
    settings.always_on_top = True
    try:
        win = main_window_mod.MainWindow(splash=_FakeSplash())
        try:
            assert win.always_on_top_action.isChecked()   # the setting was applied
            assert not win.isVisible()                    # ... without showing it
        finally:
            win.close()
    finally:
        settings.always_on_top = previous


def test_toggling_always_on_top_on_a_visible_window_keeps_it_visible(window):
    # The other half: once it IS on screen, the re-create must not lose it.
    window.show()
    window.set_always_on_top(True)
    assert window.isVisible()
    window.set_always_on_top(False)
    assert window.isVisible()


# ------------------------------------------------------------ services & logs
def test_the_log_sources_entry_became_services_and_logs(window):
    labels = {key: label for key, label, _mark in main_window_mod.CONFIGURE}
    assert labels["logsources"] == "Services & Logs"
    # The key stays "logsources": it is what Settings.page and the hidden-nav
    # list hold, so renaming it would reset somebody's rail and last page.
    assert window._pages["logsources"] is window.services


def test_the_entry_has_a_mark_of_its_own(window):
    from cms_gui import icons

    marks = {key: mark for key, _label, mark in main_window_mod.CONFIGURE}
    assert marks["logsources"] == "services"
    assert "services" in icons.DRAWINGS


def test_closing_with_nothing_running_asks_nothing(window, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("nothing is running; there is nothing to confirm")

    monkeypatch.setattr("cms_gui.pages.services.QMessageBox.question", refuse)
    assert window.services.detained() == []
    assert window._confirm_close_with_services() is True


def test_closing_on_top_of_a_service_names_it_and_can_be_called_off(window,
                                                                   monkeypatch):
    """The only thing standing between a service and being orphaned.

    PySide6 binds no setChildProcessModifier, so there is no PR_SET_PDEATHSIG:
    if this window goes first, an attached child is simply left behind. So the
    confirmation is the mechanism, not a courtesy.
    """
    from PySide6.QtWidgets import QMessageBox

    class _Doomed:
        project, name = "Claim", "Odoo Local"

    monkeypatch.setattr(window.services, "detained", lambda: [_Doomed()])
    seen = {}

    def exec_(self):
        seen["text"] = self.text() + " " + self.informativeText()
        self.setClickedButton = None
        return QMessageBox.RejectRole

    monkeypatch.setattr(QMessageBox, "exec", exec_, raising=False)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: None)
    assert window._confirm_close_with_services() is False
    assert "1 service(s) stop" in seen["text"]
    assert "Claim · Odoo Local" in seen["text"]


# --------------------------------------------------- what is running, in the footer
def _shell(name):
    from cms_gui import servicesfile as sf

    return sf.RunnerRow(name=name, type="shell", settings={
        "command": "%s -c \"import time;time.sleep(30)\"" % sys.executable})


def _with_services(window, *names):
    from cms_gui import servicesfile as sf

    window.services._projects = [sf.ProjectRow(
        name="Claim", runners=[_shell(name) for name in names])]
    window.services._rebuild()
    return window.services.supervisor


def _until(qapp, predicate, timeout=15.0):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline and not predicate():
        qapp.processEvents()
        time.sleep(0.02)
    qapp.processEvents()
    return predicate()


def test_the_footer_says_nothing_while_nothing_is_running(window):
    assert window.services_label.text() == ""


def test_the_footer_names_a_service_that_is_up(window, qapp):
    supervisor = _with_services(window, "odoo")
    try:
        window.services.start_project("Claim")
        assert _until(qapp, lambda: window.services_label.text() == "odoo: running")
    finally:
        window.services.shutdown(6000)


def test_the_footer_counts_the_rest_rather_than_filling_the_bar(window, qapp):
    # It shares its line with the machine's load and the worker limit.
    supervisor = _with_services(window, "a", "b", "c", "d")
    try:
        window.services.start_project("Claim")
        assert _until(qapp, lambda: len(supervisor.running()) == 4)
        text = window.services_label.text()
        assert text.count("running") == main_window_mod.SERVICES_IN_FOOTER
        assert text.endswith("+1")
        # The whole list is still reachable, with the project each belongs to.
        assert window.services_label.toolTip().count("Claim") == 4
    finally:
        window.services.shutdown(6000)


def test_the_footer_empties_again_when_they_stop(window, qapp):
    supervisor = _with_services(window, "odoo")
    try:
        window.services.start_project("Claim")
        assert _until(qapp, lambda: supervisor.running())
        window.services.stop_project("Claim")
        assert _until(qapp, lambda: window.services_label.text() == "")
    finally:
        window.services.shutdown(6000)


def test_the_footer_is_about_what_is_happening_not_about_settings(window):
    """Which core and which interpreter are decided once, in Settings.

    They were three fixed strings taking the left half of the status bar for
    facts that never change while the window is open. The config file stays: it
    is the one of the three that changes what a run actually reads.
    """
    assert not hasattr(window, "status_core")
    assert not hasattr(window, "status_python")
    assert window.status_config.text().startswith("config: ")
    assert os.sep not in window.status_config.text()      # the name, not the path
    assert window.status_config.toolTip()                 # the path is still here


# ------------------------------------------------- what is running, on the rail
def _badges(window, key):
    return window._nav_buttons[key].badges()


def test_the_rail_says_nothing_while_nothing_is_happening(window):
    assert _badges(window, "logsources") == [(0, "ok"), (0, "bad")]
    assert _badges(window, "run") == [(0, "ok")]


def test_the_rail_counts_the_services_that_are_up(window, qapp):
    """Visible from whichever page you are on - that is the whole point of it."""
    supervisor = _with_services(window, "a", "b")
    try:
        window.services.start_project("Claim")
        assert _until(qapp, lambda: len(supervisor.running()) == 2)
        assert _badges(window, "logsources") == [(2, "ok"), (0, "bad")]
        window.services.stop_project("Claim")
        assert _until(qapp,
                      lambda: _badges(window, "logsources") == [(0, "ok"), (0, "bad")])
    finally:
        window.services.shutdown(6000)


def test_a_service_that_fell_over_is_counted_in_red(window, qapp):
    from cms_gui import servicesfile as sf

    window.services._projects = [sf.ProjectRow(name="Claim", runners=[
        sf.RunnerRow(name="gone", type="shell", settings={
            "command": "%s -c \"import sys;sys.exit(3)\"" % sys.executable})])]
    window.services._rebuild()
    try:
        window.services.start_project("Claim")
        assert _until(qapp,
                      lambda: _badges(window, "logsources") == [(0, "ok"), (1, "bad")])
    finally:
        window.services.shutdown(6000)


def test_the_rail_counts_the_windows_a_run_has_open(window, qapp):
    window.run_state.handle({"kind": "window.launched", "session": "a", "pid": 1})
    window.run_state.handle({"kind": "window.launched", "session": "b", "pid": 2})
    # Coalesced: RunState.changed fires on every event the launcher sends.
    assert _until(qapp, lambda: _badges(window, "run") == [(2, "ok")])
    window.run_state.sessions["b"]["state"] = "passed"
    window.run_state.handle({"kind": "run.finished", "exit_code": 0})
    assert _until(qapp, lambda: _badges(window, "run") == [(1, "ok")])


def test_the_rails_run_count_clears_when_the_next_run_begins(window, qapp):
    window.run_state.handle({"kind": "window.launched", "session": "a", "pid": 1})
    assert _until(qapp, lambda: _badges(window, "run") == [(1, "ok")])
    window.run_state.reset()
    assert _until(qapp, lambda: _badges(window, "run") == [(0, "ok")])


def test_the_rail_and_the_footer_cannot_disagree_about_what_is_up(window, qapp):
    """Both read the supervisor rather than keeping a tally of their own."""
    supervisor = _with_services(window, "odoo")
    try:
        window.services.start_project("Claim")
        assert _until(qapp, lambda: window.services_label.text() == "odoo: running")
        assert _badges(window, "logsources")[0] == (len(supervisor.running()), "ok")
    finally:
        window.services.shutdown(6000)


def test_collapsing_the_rail_does_not_widen_it_for_the_counts(window, qapp):
    """The collapsed rail is the width it always was, badges or no badges.

    Room enough for a figure would be most of the rail again, and the rail is
    collapsed precisely to not be that wide - so collapsed the counts are points
    on the mark, and the numbers are on the tooltip that mode already puts the
    name on.
    """
    from PySide6.QtWidgets import QToolButton

    window.set_sidebar_collapsed(True)
    qapp.processEvents()
    rail = window._nav_buttons["launch"].parentWidget()
    collapsed = rail.width()

    # Styled and parented like the real thing, so it is measured like one: the
    # rail's rule sets the padding and the minimum height a bare button lacks.
    plain = QToolButton(rail)
    plain.setProperty("variant", "nav")
    plain.setToolButtonStyle(window._nav_buttons["logsources"].toolButtonStyle())
    plain.setIcon(window._nav_buttons["logsources"].icon())
    plain.ensurePolished()
    assert collapsed == max(main_window_mod.RAIL_COLLAPSED_MIN_WIDTH,
                            plain.sizeHint().width() + 1)

    window._nav_buttons["logsources"].set_badges([(3, "ok"), (1, "bad")])
    window.set_sidebar_collapsed(True)
    qapp.processEvents()
    assert rail.width() == collapsed


def test_the_collapsed_tooltip_carries_the_numbers_the_points_cannot(window, qapp):
    supervisor = _with_services(window, "odoo")
    try:
        window.set_sidebar_collapsed(True)
        window.services.start_project("Claim")
        assert _until(qapp, lambda: len(supervisor.running()) == 1)
        tip = window._nav_buttons["logsources"].toolTip()
        assert tip.startswith("Services & Logs")
        assert "1 running" in tip
        # Expanded the label is right there, so the tooltip drops it and keeps
        # the counts - a point may be all of them that the entry has room for.
        window.set_sidebar_collapsed(False)
        assert window._nav_buttons["logsources"].toolTip() == "1 running"
    finally:
        window.services.shutdown(6000)


def test_a_quiet_entry_keeps_the_plain_tooltip_it_always_had(window):
    window.set_sidebar_collapsed(True)
    assert window._nav_buttons["log"].toolTip() == "Log"
    assert window._nav_buttons["logsources"].toolTip() == "Services & Logs"
    # And expanded says nothing at all until there is something to say.
    window.set_sidebar_collapsed(False)
    assert window._nav_buttons["logsources"].toolTip() == ""
    assert window._nav_buttons["log"].toolTip() == ""


def test_the_counts_do_not_widen_the_rail_in_either_state(window, qapp):
    """The rail is measured from its labels. A badge may not be a reason to grow.

    Both states, because both are measured the same way - and the expanded one
    is the one a long label makes tight, which is why an entry with no room for
    a figure draws a bare point and leaves the numbers to the tooltip.
    """
    rail = window._nav_buttons["launch"].parentWidget()
    window.set_sidebar_collapsed(False)
    qapp.processEvents()
    expanded = rail.width()
    window.set_sidebar_collapsed(True)
    qapp.processEvents()
    collapsed = rail.width()

    window._nav_buttons["logsources"].set_badges([(99, "ok"), (99, "bad")])
    window._nav_buttons["run"].set_badges([(99, "ok")])
    for collapse, was in ((True, collapsed), (False, expanded)):
        window.set_sidebar_collapsed(collapse)
        qapp.processEvents()
        assert rail.width() == was, collapse


def test_reloading_the_configuration_does_not_leave_a_stale_count(window, qapp):
    """A service that stops existing reports no status on its way out.

    Loading replaces the supervisor's whole set, so the window has to ask again
    - the rail naming three running services that are no longer configured is
    worse than a rail naming none.
    """
    supervisor = _with_services(window, "a", "b")
    try:
        window.services.start_project("Claim")
        assert _until(qapp, lambda: len(supervisor.running()) == 2)
        assert _badges(window, "logsources")[0] == (2, "ok")
    finally:
        window.services.shutdown(6000)
    # What refresh_inventory does to the page, without the core behind it.
    window.services._projects = []
    window.services._rebuild()
    window._update_services()
    window._update_nav_badges()
    assert _badges(window, "logsources") == [(0, "ok"), (0, "bad")]
    assert window.services_label.text() == ""
