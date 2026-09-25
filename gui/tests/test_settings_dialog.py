"""The settings dialog: what it remembers, and what it hands the core."""

import pytest

from cms_gui import core as core_mod
from cms_gui.pages.settings_dialog import SettingsDialog
from cms_gui.settings import Settings


@pytest.fixture
def dialog(qapp):
    settings = Settings()
    # Leave nothing behind from a previous test's run.
    settings.flows_path = ""
    settings.cycle_secrets_path = ""
    settings.cycle_memory_path = ""
    settings.cycle_runs_path = ""
    return SettingsDialog(settings), settings


def test_where_the_cycle_secrets_go_is_remembered(dialog):
    dialog, settings = dialog
    dialog.cycle_secrets.setText("  /data/secrets.json  ")
    dialog.apply()
    assert settings.cycle_secrets_path == "/data/secrets.json"


def test_a_blank_secrets_field_says_where_they_would_go(dialog):
    """Blank is not an answer to "where are my credentials kept"."""
    dialog, _settings = dialog
    where = dialog.cycle_secrets.placeholderText()
    assert where.endswith("cyclesecrets.json")
    assert "cycles" not in where.split("/")       # never among the cycle files


def test_the_scenarios_folder_is_remembered(dialog):
    dialog, settings = dialog
    dialog.flows.setText("  /data/flows  ")
    dialog.apply()
    assert settings.flows_path == "/data/flows"


def test_the_scenarios_folder_reaches_the_core(dialog):
    dialog, _settings = dialog
    dialog.flows.setText("/data/flows")
    assert dialog.core().flows_dir == "/data/flows"


def test_a_blank_scenarios_folder_leaves_the_core_to_its_default(dialog):
    dialog, settings = dialog
    dialog.flows.setText("")
    dialog.apply()
    assert settings.flows_path == ""
    assert dialog.core().flows_dir == ""


def test_a_blank_field_says_where_the_scenarios_actually_go(dialog):
    # "Where do mine go right now" is the question the field exists to answer,
    # and blank is not an answer.
    dialog, _settings = dialog
    dialog.set_flows_dir("/checkout/flows")
    assert dialog.flows.placeholderText() == "/checkout/flows"


def test_a_filled_field_is_not_overwritten_by_what_is_in_force(dialog):
    dialog, _settings = dialog
    dialog.flows.setText("/mine/flows")
    dialog.set_flows_dir("/checkout/flows")
    assert dialog.flows.text() == "/mine/flows"


def test_the_dialog_opens_on_what_was_saved(qapp):
    settings = Settings()
    settings.flows_path = "/somewhere/flows"
    try:
        assert SettingsDialog(settings).flows.text() == "/somewhere/flows"
    finally:
        settings.flows_path = ""


def test_the_setting_is_what_ends_up_on_the_command_line(qapp, tmp_path):
    """--flows-dir reaches the core on every call.

    Not only on a run: the core reads the tree it is given, and the Scenarios
    page's editor and a run have to be looking at the same tree - so it has to
    be on --describe and --flow-save alike.
    """
    settings = Settings()
    settings.flows_path = ""
    dialog = SettingsDialog(settings)
    dialog.flows.setText(str(tmp_path / "flows"))
    core = dialog.core()
    assert isinstance(core, core_mod.Core)
    if core.script:
        assert "--flows-dir=%s" % (tmp_path / "flows") in core.argv("--describe")
        assert "--flows-dir=%s" % (tmp_path / "flows") in core.argv("--flow-save=alpha")


def test_which_core_runs_is_not_a_setting(qapp):
    """An installed GUI runs the core installed beside it, one started from a
    checkout runs that checkout's. There is nothing to choose, in either mode."""
    settings = Settings()
    for developer in (False, True):
        settings.developer_mode = developer
        try:
            dialog = SettingsDialog(settings)
            assert not hasattr(dialog, "script")
            assert not hasattr(dialog, "interpreter")
        finally:
            settings.developer_mode = False


def test_a_core_path_saved_by_an_earlier_version_is_forgotten(qapp):
    """It won over detection: a build once pointed at a checkout kept running
    the checkout's code with nothing on screen saying so."""
    from PySide6.QtCore import QSettings
    from cms_gui.settings import APP, ORG

    raw = QSettings(ORG, APP)
    raw.setValue("core/script", "/old/checkout/session_launcher.py")
    raw.setValue("core/interpreter", "/old/checkout/.venv/bin/python")
    raw.sync()
    Settings()
    raw = QSettings(ORG, APP)
    assert not raw.contains("core/script") and not raw.contains("core/interpreter")


def test_where_the_cycle_memory_goes_is_remembered(dialog):
    dialog, settings = dialog
    dialog.cycle_memory.setText("  /data/memory.json  ")
    dialog.apply()
    assert settings.cycle_memory_path == "/data/memory.json"


def test_where_cycle_runs_go_is_remembered(dialog):
    dialog, settings = dialog
    dialog.cycle_runs.setText("  /data/runs  ")
    dialog.apply()
    assert settings.cycle_runs_path == "/data/runs"


def test_a_blank_runs_field_does_not_guess_a_path(dialog):
    """Blank is the core's own data folder - the checkout, for a core run from
    one - so a path in the placeholder would be wrong in exactly that case."""
    dialog, _settings = dialog
    assert "core's data folder" in dialog.cycle_runs.placeholderText()


def test_the_two_cycle_stores_are_separate_fields(dialog):
    """Secrets are hidden because reading them is the harm; memory is meant to
    be read. One box for both would put the wrong policy on one of them."""
    dialog, _settings = dialog
    assert dialog.cycle_memory.placeholderText().endswith("cyclememory.json")
    assert dialog.cycle_secrets.placeholderText().endswith("cyclesecrets.json")


# ------------------------------------------------------- fitting on a screen
# Ten paths, each with a note under it, is taller than a laptop screen - and a
# dialog cannot grow past one, so Qt used to squeeze the notes below the height
# their wrapped text needs until each ran into the field under it.
def test_the_form_scrolls_when_there_is_not_room_for_all_of_it(dialog, qapp):
    dialog, _settings = dialog
    dialog.resize(620, 320)
    dialog.show()
    qapp.processEvents()
    try:
        assert dialog._scroll.widget().height() > dialog._scroll.viewport().height()
        assert dialog._scroll.verticalScrollBar().maximum() > 0
    finally:
        dialog.close()


def test_save_is_reachable_however_short_the_screen(dialog, qapp):
    """It is outside the scroll, so it cannot be the thing that is cut off."""
    from PySide6.QtWidgets import QDialogButtonBox

    dialog, _settings = dialog
    dialog.resize(620, 320)
    dialog.show()
    qapp.processEvents()
    try:
        buttons = dialog.findChild(QDialogButtonBox)
        assert buttons is not None
        assert not dialog._scroll.isAncestorOf(buttons)
        top_left = buttons.mapTo(dialog, buttons.rect().topLeft())
        assert top_left.y() + buttons.height() <= dialog.height()
    finally:
        dialog.close()


def test_a_hint_is_given_the_height_its_own_text_needs(dialog, qapp):
    """The squeeze was what made two of them overlap: a word-wrapped label
    compressed below its wrapped height draws over the field beneath it."""
    from PySide6.QtWidgets import QLabel

    dialog, _settings = dialog
    dialog.resize(620, 320)
    dialog.show()
    qapp.processEvents()
    try:
        hints = [one for one in dialog._body.findChildren(QLabel)
                 if one.wordWrap() and len(one.text()) > 80]
        assert hints
        for hint in hints:
            assert hint.height() >= hint.heightForWidth(hint.width())
    finally:
        dialog.close()
