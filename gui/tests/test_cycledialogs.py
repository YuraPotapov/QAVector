"""Editing a step, and a cycle's properties, in a window.

These were a panel down the side of the Cycles page. What is worth testing is
the same thing it always was, now that it has room: that the step's form is
**generated from the plugin's metadata** rather than written here, that what
goes in comes back out unchanged, and that a plugin's own setup actions are
offered without this file knowing what any of them mean.

The core is faked. What a plugin actually does when asked is covered by
tests/test_cycle_plugins_agent.py in the core checkout.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QLineEdit

from cms_gui.pages.cycledialogs import (PATH, SECRET, TEXT, CycleDialog,
                                         NodeDialog, _VariablesTable)


SHELL = {"id": "command.shell", "name": "Shell Command",
         "summary": "Run a command.",
         "inputs": [
             {"key": "command", "label": "Command", "kind": "multiline",
              "hint": "What to run.", "required": True, "default": None,
              "options": []},
             {"key": "dir", "label": "Working directory", "kind": "dir",
              "hint": "", "required": False, "default": None, "options": []},
             {"key": "shell", "label": "Use a shell", "kind": "check",
              "hint": "", "required": False, "default": True, "options": []},
             {"key": "expect_exit", "label": "Expected exit code",
              "kind": "number", "hint": "", "required": False, "default": 0,
              "options": []},
             {"key": "env", "label": "Environment", "kind": "env", "hint": "",
              "required": False, "default": None, "options": []},
         ],
         "outputs": [{"key": "exit_code", "type": "number", "hint": ""}],
         "actions": []}

WITH_ACTIONS = dict(SHELL, id="agent.review", actions=[
    {"key": "sign_in_status", "label": "Sign-in", "kind": "status",
     "hint": "Whether it can run."},
    {"key": "sign_in", "label": "Sign in...", "kind": "command",
     "hint": "Opens the browser."}])


def step(**extra):
    base = {"id": "build", "plugin": "command.shell",
            "with": {"command": "make all"}}
    base.update(extra)
    return base


@pytest.fixture
def node(qapp, dispose):
    made = []

    def build(one=None, plugin=SHELL, writable=True, live=None, ask=None,
              trigger=None):
        # `plugin` defaults rather than falling back, so a test can pass {} to
        # mean "this core has never heard of it".
        dialog = NodeDialog(one or step(), plugin, None, writable,
                            live=live, ask_plugin=ask, trigger=trigger)
        made.append(dialog)
        return dialog

    yield build
    for dialog in made:
        dispose(dialog)


def _buttons(dialog):
    from PySide6.QtWidgets import QPushButton
    return {button.text(): button
            for button in dialog.findChildren(QPushButton)}


# ------------------------------------------------------------- what it shows
def test_a_step_s_own_fields_are_there(node):
    dialog = node()
    for key in ("id", "label", "needs", "if", "timeout", "retry.attempts",
                "retry.delay", "on_failure", "disabled"):
        assert key in dialog._widgets, key


def test_the_plugin_s_fields_are_generated_from_its_metadata(node):
    """Nothing here knows what command.shell takes - the core said."""
    dialog = node()
    assert "with.command" in dialog._widgets
    assert "with.expect_exit" in dialog._widgets
    assert "with.env" in dialog._widgets


def test_each_kind_becomes_the_widget_it_says(node):
    from PySide6.QtWidgets import (QCheckBox, QComboBox, QLineEdit,
                                   QPlainTextEdit)

    dialog = node()
    assert isinstance(dialog._widgets["with.command"], QPlainTextEdit)
    assert isinstance(dialog._widgets["with.shell"], QCheckBox)
    assert isinstance(dialog._widgets["with.dir"], QLineEdit)
    assert isinstance(dialog._widgets["with.env"], QPlainTextEdit)
    assert isinstance(dialog._widgets["on_failure"], QComboBox)


def test_what_the_step_says_is_what_the_form_shows(node):
    dialog = node(step(label="Build it", needs=["checkout"], timeout=300,
                       retry={"attempts": 3, "delay": 1.5},
                       on_failure="continue", disabled=True))

    assert dialog._widgets["label"].text() == "Build it"
    assert dialog._widgets["needs"].text() == "checkout"
    assert dialog._widgets["timeout"].text() == "300"
    assert dialog._widgets["retry.attempts"].text() == "3"
    assert dialog._widgets["retry.delay"].text() == "1.5"
    assert dialog._widgets["on_failure"].currentData() == "continue"
    assert dialog._widgets["disabled"].isChecked()


def test_a_field_the_step_never_set_shows_the_plugin_s_default(node):
    dialog = node()
    assert dialog._widgets["with.shell"].isChecked()          # default True
    assert dialog._widgets["with.expect_exit"].text() == "0"


def test_a_plugin_the_core_never_mentioned_still_opens(node):
    """A cycle may name a plugin this core does not have."""
    dialog = node(plugin={})
    assert "id" in dialog._widgets
    assert not [key for key in dialog._widgets if key.startswith("with.")]


def test_a_bundled_cycle_s_step_cannot_be_changed(node):
    dialog = node(writable=False)
    assert not dialog.save_button.isEnabled()
    assert not dialog._widgets["label"].isEnabled()
    assert "ships with the application" in dialog.note.text()


# -------------------------------------------------------- what it gives back
def test_saving_gives_back_the_step(node):
    dialog = node()
    dialog._widgets["label"].setText("Build everything")
    dialog._save()

    assert dialog.saved["id"] == "build"
    assert dialog.saved["label"] == "Build everything"
    assert dialog.saved["with"]["command"] == "make all"


def test_what_went_in_comes_back_out_unchanged(node):
    """A round trip that quietly dropped a field would be the worst kind of
    editor: the loss is only noticed when the step next runs."""
    one = step(label="Build it", needs=["a", "b"], timeout=300,
               retry={"attempts": 3, "delay": 1.5}, on_failure="continue",
               disabled=True, **{"if": "${steps.a.status} == 'success'"})
    one["with"] = {"command": "make all", "dir": "src", "shell": False,
                   "expect_exit": 2, "env": {"A": "1"}}

    dialog = node(one)
    dialog._save()

    assert dialog.saved == one


def test_a_blank_field_is_left_out_rather_than_written_empty(node):
    """A file full of `label: ""` says less than one without them."""
    dialog = node()
    dialog._save()

    assert set(dialog.saved) == {"id", "plugin", "with"}


def test_a_step_with_no_id_is_refused(node):
    dialog = node()
    dialog._widgets["id"].setText("  ")
    dialog._save()

    assert dialog.saved is None
    assert "needs an id" in dialog.note.text()


def test_a_timeout_that_is_not_a_positive_number_is_refused(node):
    for bad in ("soon", "-5", "0"):
        dialog = node()
        dialog._widgets["timeout"].setText(bad)
        dialog._save()
        assert dialog.saved is None, bad


def test_needs_is_read_as_a_list_however_it_was_spaced(node):
    dialog = node()
    dialog._widgets["needs"].setText(" a ,b,  c ")
    dialog._save()
    assert dialog.saved["needs"] == ["a", "b", "c"]


def test_a_number_field_comes_back_as_a_number(node):
    dialog = node()
    dialog._widgets["with.expect_exit"].setText("3")
    dialog._save()
    assert dialog.saved["with"]["expect_exit"] == 3


def test_an_environment_box_comes_back_as_a_mapping(node):
    dialog = node()
    dialog._widgets["with.env"].setPlainText("A = 1\nnonsense\nB=2")
    dialog._save()
    assert dialog.saved["with"]["env"] == {"A": "1", "B": "2"}


# ----------------------------------------------- a choice that may be unset
WITH_EFFORT = dict(SHELL, inputs=SHELL["inputs"] + [
    {"key": "effort", "label": "Effort", "kind": "choice", "hint": "",
     "required": False, "default": "",
     "options": ["low", "medium", "high", "xhigh", "max"]}])


def test_a_choice_whose_default_is_blank_can_be_left_alone(node):
    """Opening a step and saving it must not choose a level nobody asked for."""
    dialog = node(plugin=WITH_EFFORT)
    assert dialog._widgets["with.effort"].currentData() == ""
    dialog._save()
    assert "effort" not in dialog.saved["with"]


def test_the_blank_option_reads_as_the_default_rather_than_an_empty_row(node):
    dialog = node(plugin=WITH_EFFORT)
    assert dialog._widgets["with.effort"].itemText(0) == "(default)"


def test_a_choice_that_was_set_comes_back_as_it_was(node):
    one = step()
    one["with"] = {"command": "make all", "effort": "xhigh"}
    dialog = node(one, plugin=WITH_EFFORT)
    dialog._save()
    assert dialog.saved["with"]["effort"] == "xhigh"


def test_a_choice_this_build_does_not_offer_is_kept(node):
    """Silently resetting it would lose it on the next save."""
    dialog = node(step(on_failure="something_new"))
    dialog._save()
    assert dialog.saved["on_failure"] == "something_new"


# ------------------------------------------------------------- setting it up
def test_a_plugin_that_declares_actions_gets_them_offered(node):
    """Rendered from the metadata alone - the dialog has no idea what they do."""
    dialog = node(plugin=WITH_ACTIONS, ask=lambda key, settings: {"ok": True})
    assert "Check" in _buttons(dialog)
    assert "Sign in..." in _buttons(dialog)


def test_a_plugin_that_declares_none_gets_no_setup_section(node):
    dialog = node(ask=lambda key, settings: {"ok": True})
    assert "Check" not in _buttons(dialog)


def test_nothing_is_asked_until_somebody_presses_it(node):
    """Opening a step must not start a subprocess."""
    asked = []
    node(plugin=WITH_ACTIONS, ask=lambda key, settings: asked.append(key))
    assert asked == []


def test_checking_asks_with_what_the_form_says_now(node):
    """Somebody who changed the backend and then pressed Check means the new
    one - not what the file still says."""
    asked = []

    def ask(key, settings):
        asked.append((key, settings))
        return {"ok": True, "summary": "ready", "detail": "", "argv": []}

    dialog = node(plugin=WITH_ACTIONS, ask=ask)
    dialog._widgets["with.command"].setPlainText("changed on screen")
    _buttons(dialog)["Check"].click()

    assert asked[0][0] == "sign_in_status"
    assert asked[0][1]["command"] == "changed on screen"


def test_a_command_action_runs_what_the_plugin_handed_back(node, monkeypatch):
    started = []
    monkeypatch.setattr("subprocess.Popen",
                        lambda argv, **kwargs: started.append(list(argv)))

    dialog = node(plugin=WITH_ACTIONS,
                  ask=lambda key, settings: {"ok": True, "summary": "go",
                                             "detail": "", "argv": ["/bin/true"]})
    _buttons(dialog)["Sign in..."].click()
    assert started == [["/bin/true"]]


def test_a_command_the_plugin_will_not_give_is_not_run(node, monkeypatch):
    started = []
    monkeypatch.setattr("subprocess.Popen",
                        lambda argv, **kwargs: started.append(list(argv)))

    dialog = node(plugin=WITH_ACTIONS,
                  ask=lambda key, settings: {"ok": False, "summary": "not here",
                                             "detail": "", "argv": []})
    _buttons(dialog)["Sign in..."].click()
    assert started == []


def test_a_command_that_cannot_start_is_reported_rather_than_raising(node,
                                                                     monkeypatch):
    def refuse(argv, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr("subprocess.Popen", refuse)
    dialog = node(plugin=WITH_ACTIONS,
                  ask=lambda key, settings: {"ok": True, "summary": "go",
                                             "detail": "", "argv": ["/nope"]})
    _buttons(dialog)["Sign in..."].click()      # must not raise


# ------------------------------------------------------------- what it ran to
def test_what_a_run_did_is_shown_when_there_is_a_run(node):
    dialog = node(live={"status": "failed", "duration_ms": 2500, "attempt": 2,
                        "message": "exited 1", "outputs": {"exit_code": 1}})
    text = " ".join(one.text() for one in dialog.findChildren(object)
                    if hasattr(one, "text") and callable(one.text)
                    and isinstance(one.text(), str))

    assert "failed" in text and "exited 1" in text and "exit_code" in text


def _said(dialog):
    """Every piece of text on a dialog, as one string."""
    return " ".join(one.text() for one in dialog.findChildren(object)
                    if hasattr(one, "text") and callable(one.text)
                    and isinstance(one.text(), str))


def test_a_structured_output_is_counted_rather_than_dumped_as_a_repr(node):
    """An agent's issues printed as a Python repr is the wall of text the
    Stages tab exists to replace - and it was cut off mid-word by the edge."""
    dialog = node(live={"status": "success", "outputs": {
        "issues": [{"severity": "low", "description": "Unused import"},
                   {"severity": "high", "description": "MD5 without a salt"}],
        "recommendations": ["Use a slow KDF"]}})
    text = _said(dialog)

    assert "2 items" in text and "1 item" in text
    assert "MD5 without a salt" not in text
    assert "[{'" not in text


def test_the_names_are_still_there_because_a_later_step_references_them(node):
    """${steps.review.outputs.issues} has to be discoverable from here."""
    dialog = node(live={"status": "success",
                        "outputs": {"issues": [], "report_path": "a/b.json"}})
    text = _said(dialog)
    assert "issues" in text and "report_path" in text
    assert "a/b.json" in text           # a scalar is still worth showing whole


def test_a_step_with_only_scalar_outputs_gets_no_pointer_to_the_stages(node):
    dialog = node(live={"status": "success", "outputs": {"exit_code": 0}})
    assert "Stages tab" not in _said(dialog)


def test_a_very_long_scalar_output_is_cut_rather_than_running_off_the_edge(node):
    dialog = node(live={"status": "success",
                        "outputs": {"summary": "word " * 200}})
    assert "..." in _said(dialog)


def test_a_step_that_has_not_run_says_nothing_about_a_run(node):
    for live in (None, {}, {"status": "pending"}):
        dialog = node(live=live)
        text = " ".join(one.text() for one in dialog.findChildren(object)
                        if hasattr(one, "text") and callable(one.text)
                        and isinstance(one.text(), str))
        assert "THIS RUN" not in text.upper()


# ---------------------------------------------------------------- the cycle
@pytest.fixture
def properties(qapp, dispose):
    made = []

    def build(document=None, projects=("Portal", "Release"), writable=True,
              stored_secrets=()):
        dialog = CycleDialog(document or {"id": "demo", "name": "Demo",
                                          "project": "Portal",
                                          "variables": {"branch": "main"}},
                             projects, None, writable,
                             stored_secrets=stored_secrets)
        made.append(dialog)
        return dialog

    yield build
    for dialog in made:
        dispose(dialog)


def _row(dialog, name):
    """Which row of the variables table carries ``name``. They are sorted."""
    table = dialog.variables.table
    for row in range(table.rowCount()):
        if table.cellWidget(row, 0).text() == name:
            return row
    raise AssertionError("no variable called %r; there are %s"
                         % (name, [table.cellWidget(r, 0).text()
                                   for r in range(table.rowCount())]))


def _set(dialog, name, value=None, kind=None):
    """Type into one row the way somebody would."""
    table, row = dialog.variables.table, _row(dialog, name)
    if kind is not None:
        table.cellWidget(row, 2).setCurrentText(kind)
    if value is not None:
        table.cellWidget(row, 1).setText(value)


def test_the_cycle_s_own_properties_are_here_not_on_a_step(properties):
    """A variable is one per cycle; a step only reads it."""
    dialog = properties()
    assert dialog.name.text() == "Demo"
    assert dialog.project.currentData() == "Portal"
    table = dialog.variables.table
    assert table.cellWidget(_row(dialog, "branch"), 0).text() == "branch"
    assert table.cellWidget(_row(dialog, "branch"), 1).text() == "main"


def test_variables_come_back_as_a_mapping(properties):
    dialog = properties()
    _set(dialog, "branch", "release")
    dialog.variables.add_row("jobs", "4", TEXT)
    dialog._save()
    assert dialog.saved["variables"] == {"branch": "release", "jobs": "4"}


def test_a_row_without_a_name_is_dropped_rather_than_saved_as_one(properties):
    """Clicking Add and then thinking better of it must not write a variable."""
    dialog = properties()
    dialog.variables.add_row("   ", "orphan", TEXT)
    dialog._save()
    assert dialog.saved["variables"] == {"branch": "main"}


def test_a_variable_can_be_removed(properties):
    dialog = properties()
    dialog.variables.table.removeRow(_row(dialog, "branch"))
    dialog._save()
    assert dialog.saved["variables"] == {}


# ------------------------------------------------------------------ secrets
def test_a_secret_declares_itself_in_the_file_and_keeps_its_value_out(properties):
    """The whole point: the cycle file gets the name, the store gets the value."""
    dialog = properties()
    _set(dialog, "branch", "s3cr3t", SECRET)
    dialog._save()
    assert dialog.saved["variables"] == {"branch": {"secret": True}}
    assert dialog.secret_values == {"branch": "s3cr3t"}


def test_a_secret_s_box_never_shows_what_is_in_it(properties):
    dialog = properties()
    _set(dialog, "branch", kind=SECRET)
    box = dialog.variables.table.cellWidget(_row(dialog, "branch"), 1)
    assert box.echoMode() == QLineEdit.Password
    _set(dialog, "branch", kind=TEXT)
    assert box.echoMode() == QLineEdit.Normal


def test_a_stored_secret_opens_empty_and_says_so(properties):
    """The GUI is told a secret is set, never what it is - so there is nothing
    to put in the box, and the box says which it is."""
    dialog = properties({"id": "demo", "variables": {"token": {"secret": True}}},
                        stored_secrets=("token",))
    box = dialog.variables.table.cellWidget(_row(dialog, "token"), 1)
    assert box.text() == ""
    assert "stored" in box.placeholderText()


def test_a_secret_left_alone_is_not_written_over_with_a_blank(properties):
    dialog = properties({"id": "demo", "variables": {"token": {"secret": True}}},
                        stored_secrets=("token",))
    dialog._save()
    assert dialog.saved["variables"] == {"token": {"secret": True}}
    assert dialog.secret_values == {}       # nothing to write; keep what is there
    assert dialog.dropped_secrets == []


def test_a_secret_turned_back_into_text_is_dropped_from_the_store(properties):
    """Otherwise the old value sits there unreadable and undeletable."""
    dialog = properties({"id": "demo", "variables": {"token": {"secret": True}}},
                        stored_secrets=("token",))
    _set(dialog, "token", "plain", TEXT)
    dialog._save()
    assert dialog.saved["variables"] == {"token": "plain"}
    assert dialog.dropped_secrets == ["token"]


def test_a_secret_turned_into_a_path_is_dropped_from_the_store(properties):
    dialog = properties({"id": "demo", "variables": {"repo": {"secret": True}}},
                        stored_secrets=("repo",))
    _set(dialog, "repo", "/src/app", PATH)
    dialog._save()
    assert dialog.saved["variables"] == {
        "repo": {"kind": PATH, "value": "/src/app"}}
    assert dialog.secret_values == {}
    assert dialog.dropped_secrets == ["repo"]


@pytest.mark.parametrize("remove", [False, True])
def test_editing_a_path_does_not_delete_a_secret(properties, remove):
    dialog = properties({"id": "demo", "variables": {
        "repo": {"kind": PATH, "value": "/src/app"}}})
    if remove:
        dialog.variables.table.removeRow(_row(dialog, "repo"))
    else:
        _set(dialog, "repo", kind=TEXT)
    dialog._save()
    assert dialog.secret_values == {}
    assert dialog.dropped_secrets == []


def test_a_bundled_cycle_s_variables_cannot_be_typed_into(properties):
    """And the two buttons go, rather than sitting there looking live."""
    dialog = properties(writable=False)
    table = dialog.variables
    assert not table.isEnabled()
    assert not table.table.cellWidget(0, 0).isEnabled()
    # isHidden, not isVisible: nothing here is ever shown, so isVisible would
    # be False for a button that was left perfectly visible.
    assert table.add_button.isHidden() and table.remove_button.isHidden()
    assert not properties().variables.add_button.isHidden()


def test_a_removed_secret_is_dropped_from_the_store_too(properties):
    dialog = properties({"id": "demo", "variables": {"token": {"secret": True}}},
                        stored_secrets=("token",))
    dialog.variables.table.removeRow(_row(dialog, "token"))
    dialog._save()
    assert dialog.saved["variables"] == {}
    assert dialog.dropped_secrets == ["token"]


def test_a_cycle_can_be_moved_to_another_project(properties):
    dialog = properties()
    dialog.project.setCurrentIndex(dialog.project.findData("Release"))
    dialog._save()
    assert dialog.saved["project"] == "Release"


def test_a_cycle_can_be_taken_out_of_every_project(properties):
    dialog = properties()
    dialog.project.setCurrentIndex(0)          # Unassigned
    dialog._save()
    assert dialog.saved["project"] == ""


def test_a_project_the_file_never_heard_of_is_offered_as_it_is(properties):
    dialog = properties({"id": "demo", "project": "Gone"})
    assert dialog.project.currentData() == "Gone"


def test_a_bundled_cycle_s_properties_cannot_be_changed(properties):
    dialog = properties(writable=False)
    assert not dialog.save_button.isEnabled()
    assert not dialog.name.isEnabled()


# ------------------------------------------------- choosing a path, not typing
# Most of a cycle's variables are paths - the checkout it works in, where the
# rules are written down - and typing one by hand is how a cycle ends up
# pointed at a directory that is nearly right.
def test_only_a_path_offers_a_folder_chooser(qapp, dispose):
    """On a token it would invite exactly the wrong thing; on ordinary text it
    is a button beside a value nobody is choosing a folder for."""
    table = _VariablesTable()
    try:
        table.add_row("project_dir", "/path/to/checkout", kind=PATH)
        table.add_row("jira_user", "currentUser()")
        table.add_row("jira_token", "", kind=SECRET, stored=True)

        assert table.table.cellWidget(0, 1).browse.isVisible()
        assert not table.table.cellWidget(1, 1).browse.isVisible()
        assert not table.table.cellWidget(2, 1).browse.isVisible()
    finally:
        dispose(table)


def test_a_secret_offers_nothing_to_browse_for(qapp, dispose):
    """Nothing to choose for a token, and an echo-hidden field with a folder
    button beside it invites exactly the wrong thing."""
    table = _VariablesTable()
    try:
        table.add_row("jira_token", "", kind="secret", stored=True)
        box = table.table.cellWidget(0, 1)
        assert not box.browse.isVisible()
    finally:
        dispose(table)


def test_the_chooser_follows_the_type(qapp, dispose):
    table = _VariablesTable()
    try:
        table.add_row("project_dir", "/path")
        box = table.table.cellWidget(0, 1)
        choice = table.table.cellWidget(0, 2)
        assert not box.browse.isVisible(), "text to begin with"

        choice.setCurrentText(PATH)
        assert box.browse.isVisible()

        choice.setCurrentText(SECRET)
        assert not box.browse.isVisible()

        choice.setCurrentText(PATH)
        assert box.browse.isVisible()
    finally:
        dispose(table)


def test_choosing_a_folder_puts_it_in_the_row(qapp, dispose, monkeypatch):
    from cms_gui import widgets

    table = _VariablesTable()
    try:
        table.add_row("project_dir", "/old", kind=PATH)
        box = table.table.cellWidget(0, 1)
        monkeypatch.setattr(widgets, "pick_path", lambda *a, **k: "/chosen")

        box.browse.trigger()
        assert box.text() == "/chosen"
        assert table.rows()[0]["value"] == "/chosen"
    finally:
        dispose(table)


def test_changing_your_mind_leaves_the_value_alone(qapp, dispose, monkeypatch):
    """A chooser that emptied the field when somebody cancelled would be worse
    than no chooser."""
    from cms_gui import widgets

    table = _VariablesTable()
    try:
        table.add_row("project_dir", "/old", kind=PATH)
        box = table.table.cellWidget(0, 1)
        monkeypatch.setattr(widgets, "pick_path", lambda *a, **k: "")

        box.browse.trigger()
        assert box.text() == "/old"
    finally:
        dispose(table)


def test_the_chooser_starts_where_the_value_points(qapp, dispose, monkeypatch):
    """Opening in the home directory when the field already names a checkout
    means finding it again every time."""
    from cms_gui import widgets

    seen = {}

    def picked(_parent, _title, start="", **kwargs):
        seen["start"] = start
        seen["directory"] = kwargs.get("directory")
        return ""

    table = _VariablesTable()
    try:
        table.add_row("project_dir", "/somewhere/real", kind=PATH)
        monkeypatch.setattr(widgets, "pick_path", picked)
        table.table.cellWidget(0, 1).browse.trigger()

        assert seen["start"] == "/somewhere/real"
        assert seen["directory"] is True
    finally:
        dispose(table)


def test_a_path_survives_the_round_trip_through_the_form(qapp, dispose):
    """A cycle opened and saved without being touched must come back the same.
    A path quietly demoted to ordinary text would lose its chooser on the next
    open, which reads as the feature having broken."""
    document = {"id": "c", "name": "C", "variables": {
        "branch": "main",
        "repo": {"kind": PATH, "value": "/src/app"},
        "token": {"secret": True},
    }, "steps": []}
    dialog = CycleDialog(document, ["Demo"])
    try:
        dialog._save()
        assert dialog.saved["variables"] == {
            "branch": "main",
            "repo": {"kind": PATH, "value": "/src/app"},
            "token": {"secret": True},
        }
    finally:
        dispose(dialog)


def test_a_path_row_is_filled_from_the_declaration(qapp, dispose):
    table = _VariablesTable()
    try:
        table.load({"repo": {"kind": PATH, "value": "/src/app"}})
        assert table.table.cellWidget(0, 1).text() == "/src/app"
        assert table.table.cellWidget(0, 2).currentText() == PATH
    finally:
        dispose(table)


def test_turning_text_into_a_path_keeps_the_value(qapp, dispose):
    """The value is the same string either way - only how the row helps you
    fill it in changes."""
    document = {"id": "c", "name": "C",
                "variables": {"repo": "/src/app"}, "steps": []}
    dialog = CycleDialog(document, ["Demo"])
    try:
        dialog.variables.table.cellWidget(0, 2).setCurrentText(PATH)
        dialog._save()
        assert dialog.saved["variables"] == {"repo": {"kind": PATH,
                                                      "value": "/src/app"}}
    finally:
        dispose(dialog)


def test_turning_a_path_back_into_text_keeps_the_value_too(qapp, dispose):
    document = {"id": "c", "name": "C", "variables": {
        "repo": {"kind": PATH, "value": "/src/app"}}, "steps": []}
    dialog = CycleDialog(document, ["Demo"])
    try:
        dialog.variables.table.cellWidget(0, 2).setCurrentText(TEXT)
        dialog._save()
        assert dialog.saved["variables"] == {"repo": "/src/app"}
    finally:
        dispose(dialog)


def test_a_path_is_never_mistaken_for_a_secret(qapp, dispose):
    """Both are mappings in the file, and confusing them would either hide a
    path's value or write a token into a committed file."""
    document = {"id": "c", "name": "C", "variables": {
        "repo": {"kind": PATH, "value": "/src/app"}}, "steps": []}
    dialog = CycleDialog(document, ["Demo"])
    try:
        dialog._save()
        assert dialog.secret_values == {}
        assert dialog.dropped_secrets == []
        box = dialog.variables.table.cellWidget(0, 1)
        assert box.echoMode() != box.EchoMode.Password
    finally:
        dispose(dialog)



# ------------------------------------------------------ listening for new work
WATCHABLE = dict(SHELL, id="test.queue", watchable=True)


def _listen(dialog):
    return dialog._widgets.get("listen.enabled"), dialog._widgets.get("listen.every")


def test_only_a_step_that_can_be_listened_to_offers_it(node):
    assert _listen(node(plugin=SHELL)) == (None, None)
    enabled, every = _listen(node(plugin=WATCHABLE))
    assert enabled is not None and not enabled.isChecked()
    assert every.text() == "300"


def test_a_listening_step_shows_its_schedule_and_can_be_turned_off(node):
    dialog = node(plugin=WATCHABLE,
                  trigger={"id": "new_task", "watch": "build", "every": 120})
    enabled, every = _listen(dialog)
    assert enabled.isChecked() and every.text() == "120"
    enabled.setChecked(False)
    dialog._save()
    assert dialog.saved_trigger == {"enabled": False, "every": 120}


def test_turning_it_on_asks_for_a_sensible_schedule(node):
    dialog = node(plugin=WATCHABLE)
    enabled, every = _listen(dialog)
    enabled.setChecked(True)
    every.setText("5")
    dialog._save()
    assert dialog.saved is None and "60 at the least" in dialog.note.text()
    every.setText("90")
    dialog._save()
    assert dialog.saved_trigger == {"enabled": True, "every": 90}


def test_a_step_never_listened_to_says_nothing_about_it(node):
    dialog = node(plugin=WATCHABLE)
    dialog._save()
    assert dialog.saved is not None and dialog.saved_trigger is None


def test_the_file_keeps_an_off_trigger_and_follows_a_renamed_step():
    from cms_gui.pages.cycles import _with_listening

    document = {"id": "c", "steps": [{"id": "todo"}],
                "triggers": [{"id": "new_task", "watch": "todo", "every": 300}]}
    off = _with_listening(document, "todo", "take", {"enabled": False, "every": 300})
    assert off["triggers"] == [{"id": "new_task", "watch": "take", "every": 300,
                                "enabled": False}]
    on = _with_listening(off, "take", "take", {"enabled": True, "every": 120})
    assert on["triggers"] == [{"id": "new_task", "watch": "take", "every": 120}]
    fresh = _with_listening({"id": "c", "steps": []}, "q", "q",
                            {"enabled": True, "every": 300})
    assert fresh["triggers"] == [{"id": "listen_q", "watch": "q", "every": 300}]
    assert "triggers" not in _with_listening({"id": "c"}, "q", "q", None)
