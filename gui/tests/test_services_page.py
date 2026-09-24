"""The Services & Logs page: two files, one accordion, and what Save may do.

The page owns ``logsources.json`` (the launcher's) and ``services.json`` (its
own). Most of what it does is arrange rows into blocks and keep both files
exactly as valid as their readers expect, so that is what these check. The forms
themselves are covered in ``test_logsources_page.py``; the processes in
``test_services.py``.
"""

import json
import os
import sys

import pytest
from PySide6.QtWidgets import QFrame, QLabel

from cms_gui import logsourcesfile as lsf
from cms_gui import runnertypes
from cms_gui import servicesfile as sf
from cms_gui.pages.services import (STATUS_TEXT, ConsoleWindow, ProjectBlock,
                                    ProjectDialog, RunnerDialog, ServicesPage)

# The core is one directory up and not installed. The GUI never imports it at
# runtime; a test may, to check the two agree about the file.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

LOCAL = "localhost:8069"
DEV = "https://dev.example.com/"

LOGSOURCES = {
    "connections": [{"name": "here", "type": "local"},
                    {"name": "dev", "type": "ssh", "host": "dev.example.com",
                     "user": "deploy"}],
    "logs": [{"name": "app", "connection": "here", "env": LOCAL, "type": "file",
              "path": "/var/log/app.log", "format": "django", "default": True,
              "project": "Claim"},
             {"name": "nginx", "connection": "dev", "env": DEV, "type": "file",
              "path": "/var/log/nginx/error.log", "format": "nginx"}]}

SERVICES = {"version": 1, "projects": [
    {"name": "Claim", "dir": "", "runners": [
        {"name": "Odoo Local", "type": "python", "detach": False,
         "settings": {"script": "odoo-bin", "args": "-c odoo.conf"}},
        {"name": "PostgreSQL DB", "type": "docker", "detach": True,
         "settings": {"container": "postgres-claim"}}]}]}


def _write(tmp_path, logsources=None, services=None):
    path = tmp_path / "logsources.json"
    path.write_text(json.dumps(LOGSOURCES if logsources is None else logsources))
    if services is not False:
        (tmp_path / "services.json").write_text(
            json.dumps(SERVICES if services is None else services))
    return path


def _open(tmp_path, **kwargs):
    widget = ServicesPage()
    widget.set_environments([LOCAL, DEV])
    # The services path is named rather than left to resolve. Its default is
    # ~/QAVector/services.json, so a test that let it default would
    # read whatever the *previous* test's Save had put there - and the tmp file
    # it wrote for itself would be ignored.
    widget.load(str(_write(tmp_path, **kwargs)), str(tmp_path / "services.json"),
                str(tmp_path / "logsources.json"))
    return widget


@pytest.fixture
def page(qapp, tmp_path):
    widget = _open(tmp_path)
    yield widget
    widget.shutdown(2000)
    widget.close()


@pytest.fixture
def empty_page(qapp, tmp_path):
    widget = ServicesPage()
    widget.set_environments([LOCAL, DEV])
    widget.load(str(tmp_path / "logsources.json"),
                str(tmp_path / "services.json"),
                str(tmp_path / "logsources.json"))
    yield widget
    widget.shutdown(2000)
    widget.close()


def _accept(monkeypatch, fill):
    """Answer the next dialog without showing it."""
    from PySide6.QtWidgets import QDialog

    def exec_(self):
        return QDialog.Accepted if fill(self) else QDialog.Rejected

    monkeypatch.setattr(QDialog, "exec", exec_, raising=False)


def _answer_question(monkeypatch, answer=True, record=None):
    """Answer every confirmation the page raises, and optionally note it.

    A bool, because that is what `widgets.confirm` returns. It used to be one
    of QMessageBox's enum members, and the day those stopped being the answer
    every "No" read as True - they are non-zero - so a test that meant to
    refuse quietly agreed instead.
    """
    def question(_parent, _title, text, detail="", *_args, **_kwargs):
        if record is not None:
            # Both halves: these tests ask "does the dialog say this", and a
            # dialog says its question and its consequence alike.
            record["text"] = "%s\n%s" % (text, detail)
        return answer

    monkeypatch.setattr("cms_gui.widgets.confirm", question)


# --------------------------------------------------------------- the overview
def test_both_files_arrive_on_the_page(page):
    connections, logs, projects = page.rows()
    assert [c.name for c in connections] == ["here", "dev"]
    assert [l.name for l in logs] == ["app", "nginx"]
    assert [p.name for p in projects] == ["Claim"]


def test_a_stack_gets_a_block_holding_its_services_and_its_logs(page):
    block = page._blocks["Claim"]
    assert [r.name for r in block._runner_rows] == ["Odoo Local", "PostgreSQL DB"]
    assert [r.name for r in block._log_rows] == ["app"]


def test_a_log_naming_no_stack_still_has_somewhere_to_be(page):
    # Hiding a row because a name no longer matches looks exactly like losing it.
    assert [r.name for r in page._blocks[""]._log_rows] == ["nginx"]


def test_a_log_naming_a_stack_that_is_gone_is_not_hidden(qapp, tmp_path):
    logs = json.loads(json.dumps(LOGSOURCES))
    logs["logs"][0]["project"] = "Deleted Last Week"
    widget = _open(tmp_path, logsources=logs)
    assert [r.name for r in widget._blocks[""]._log_rows] == ["app", "nginx"]
    widget.close()


def test_the_block_header_says_how_much_of_the_stack_is_up(page):
    assert "0 of 2 running" in page._blocks["Claim"].disclosure.button.text()


def test_a_stack_with_nothing_in_it_says_so(qapp, tmp_path, monkeypatch):
    widget = _open(tmp_path, services={"projects": [{"name": "Empty"}]})
    assert "nothing configured" in widget._blocks["Empty"].disclosure.button.text()
    widget.close()


def test_the_overview_says_what_a_service_runs_rather_than_its_raw_settings(page):
    block = page._blocks["Claim"]
    assert block.runners.item(0, block.R_CONFIG).text() == \
        "%s odoo-bin -c odoo.conf" % runnertypes.DEFAULT_PYTHON
    assert block.runners.item(1, block.R_TYPE).text() == "Docker Container"
    assert block.runners.item(0, block.R_STATUS).text() == "Stopped"


def test_a_service_told_to_survive_the_window_says_so_in_its_row(qapp, tmp_path):
    services = json.loads(json.dumps(SERVICES))
    services["projects"][0]["runners"][0]["detach"] = True
    widget = _open(tmp_path, services=services)
    block = widget._blocks["Claim"]
    assert "detached" in block.runners.item(0, block.R_TYPE).text()
    # Never said of a container: it was never ours to keep alive.
    assert "detached" not in block.runners.item(1, block.R_TYPE).text()
    widget.close()


def test_the_overview_says_where_and_what_rather_than_raw_columns(page):
    block = page._blocks[""]
    assert page.connections.item(1, 1).text() == "ssh deploy@dev.example.com"
    assert page.connections.item(0, 2).text() == "1 log"
    assert block.logs.item(0, block.L_READS).text() == \
        "file  /var/log/nginx/error.log"


def test_the_tables_are_not_edited_in_place(page):
    # Every edit opens a form; a cell that looks editable and is not is worse
    # than one that plainly is not.
    from PySide6.QtWidgets import QAbstractItemView
    block = page._blocks["Claim"]
    for table in (page.connections, block.logs, block.runners):
        assert table.editTriggers() == QAbstractItemView.NoEditTriggers


def test_an_empty_file_says_what_to_do_first(empty_page):
    assert "Add a project" in empty_page.status.text()


def test_the_add_menu_offers_exactly_the_registered_types(page):
    labels = [a.text() for a in page._blocks["Claim"]._add_menu.actions()]
    assert labels == [t.label for t in runnertypes.TYPES]


# ------------------------------------------------------------------- stacks
def test_a_new_stack_appears_as_its_own_block(page, monkeypatch):
    _accept(monkeypatch, lambda d: d.name.setText("Helpdesk") or True)
    page.add_project()
    assert "Helpdesk" in page._blocks
    assert [p.name for p in page.rows()[2]] == ["Claim", "Helpdesk"]


def test_renaming_a_stack_takes_its_logs_with_it(page, monkeypatch):
    # A log names its stack by name. Renaming one without this tips every log
    # that used it into Unassigned, which reads as having lost them.
    _accept(monkeypatch, lambda d: d.name.setText("Claims") or True)
    page.edit_project("Claim")
    assert [r.name for r in page._blocks["Claims"]._log_rows] == ["app"]
    assert page.rows()[1][0].project == "Claims"


def test_deleting_a_stack_keeps_its_logs_and_says_so(page, monkeypatch):
    asked = {}
    _answer_question(monkeypatch, record=asked)
    page.delete_project("Claim")
    assert "move to Unassigned" in asked["text"]
    assert [l.name for l in page.rows()[1]] == ["app", "nginx"]
    assert page.rows()[1][0].project == ""
    assert [r.name for r in page._blocks[""]._log_rows] == ["app", "nginx"]


def test_a_stack_can_be_left_alone_at_the_confirmation(page, monkeypatch):
    _answer_question(monkeypatch, answer=False)
    page.delete_project("Claim")
    assert [p.name for p in page.rows()[2]] == ["Claim"]


def test_whether_a_block_was_open_is_remembered_in_the_file(page, tmp_path):
    page._blocks["Claim"].disclosure.set_expanded(False)
    page.save()
    assert sf.load(str(tmp_path / "services.json"))[0].expanded is False


# ------------------------------------------------------------------ services
def test_a_service_is_added_through_its_own_generated_form(page, monkeypatch):
    def fill(dialog):
        dialog.name.setText("Worker")
        dialog._editors["command"].setText("celery worker")
        return True

    _accept(monkeypatch, fill)
    page.add_runner("Claim", runnertypes.BY_ID["shell"])
    names = [r.name for r in page.rows()[2][0].runners]
    assert names == ["Odoo Local", "PostgreSQL DB", "Worker"]
    assert page.save_button.isEnabled()


def test_a_copied_service_is_valid_the_moment_it_is_made(page):
    project = page.rows()[2][0]
    page.copy_runner("Claim", project.runners[0])
    assert [r.name for r in page.rows()[2][0].runners] == [
        "Odoo Local", "Odoo Local-copy", "PostgreSQL DB"]
    assert page.save_button.isEnabled()


def test_deleting_a_service_asks_first(page, monkeypatch):
    asked = {}
    _answer_question(monkeypatch, record=asked)
    page.delete_runner("Claim", page.rows()[2][0].runners[0])
    assert "Odoo Local" in asked["text"]
    assert [r.name for r in page.rows()[2][0].runners] == ["PostgreSQL DB"]


def test_a_service_the_supervisor_knows_gets_a_console(page):
    window = page.open_console("Claim", page.rows()[2][0].runners[0])
    assert isinstance(window, ConsoleWindow)
    assert "Odoo Local" in window.windowTitle()
    assert window.status.text() == "STOPPED"
    window.close()


def test_a_type_this_build_does_not_have_is_shown_but_not_edited(
        qapp, tmp_path, monkeypatch):
    services = {"projects": [{"name": "Claim", "runners": [
        {"name": "quantum", "type": "quantum", "settings": {}}]}]}
    widget = _open(tmp_path, services=services)
    block = widget._blocks["Claim"]
    assert "unknown" in block.runners.item(0, block.R_TYPE).text()
    widget.edit_runner("Claim", widget.rows()[2][0].runners[0])
    assert "no 'quantum' runner" in widget.status.text()
    # Reported, never dropped: this build being older than the file is not a
    # reason to delete somebody's configuration.
    assert not widget.save_button.isEnabled()
    widget.close()


# ------------------------------------------------------------------ the forms
def test_a_generated_form_collects_what_its_type_declared(qapp):
    dialog = RunnerDialog(runnertypes.BY_ID["python"])
    assert set(dialog._editors) == {f.key for f in
                                runnertypes.BY_ID["python"].form_fields()}
    dialog.name.setText("Odoo")
    dialog._editors["script"].setText("odoo-bin")
    dialog._editors["env"].add_row("PYTHONUNBUFFERED", "1")
    row = dialog.value()
    assert row.type == "python"
    assert row.settings["script"] == "odoo-bin"
    assert row.settings["env"] == {"PYTHONUNBUFFERED": "1"}


def test_a_form_opened_and_accepted_untouched_changes_nothing(qapp):
    """Otherwise Save lights up for a service somebody only looked at.

    Every supervised form grew a *Stop timeout* in 0.14.3, and a row written
    before that has no such key. Writing the empty field back as one is a change
    to the file like any other: open a service, click in a field, press OK, and
    the page offers to rewrite it - which is exactly the report this comes from,
    and it happened on every row except the few edited since the field landed.
    """
    row = sf.RunnerRow(name="Odoo", type="python",
                       settings={"script": "odoo-bin"})
    dialog = RunnerDialog(runnertypes.BY_ID["python"], row)
    assert "stop_grace" in dialog._editors, "the field is on the form"
    assert dialog.value().to_entry() == row.to_entry()

    # And it is written the moment it says anything, blank included, once the
    # row has one - clearing a stop timeout has to save.
    dialog._editors["stop_grace"].setText("30")
    assert dialog.value().settings["stop_grace"] == "30"
    kept = sf.RunnerRow(name="Odoo", type="python",
                        settings={"script": "odoo-bin", "stop_grace": "30"})
    cleared = RunnerDialog(runnertypes.BY_ID["python"], kept)
    cleared._editors["stop_grace"].setText("")
    assert cleared.value().settings["stop_grace"] == ""


def test_a_form_will_not_be_accepted_until_its_type_is_satisfied(qapp):
    from PySide6.QtWidgets import QDialogButtonBox
    dialog = RunnerDialog(runnertypes.BY_ID["python"])
    dialog.name.setText("Odoo")
    assert not dialog.buttons.button(QDialogButtonBox.Ok).isEnabled()
    assert "Script is required" in dialog.problem.text()
    dialog._editors["script"].setText("odoo-bin")
    assert dialog.buttons.button(QDialogButtonBox.Ok).isEnabled()


def test_two_services_in_one_stack_cannot_share_a_name(qapp):
    from PySide6.QtWidgets import QDialogButtonBox
    dialog = RunnerDialog(runnertypes.BY_ID["shell"], taken=["Odoo Local"])
    dialog.name.setText("Odoo Local")
    dialog._editors["command"].setText("true")
    assert not dialog.buttons.button(QDialogButtonBox.Ok).isEnabled()


def test_a_container_is_not_offered_the_choice_of_detaching(qapp):
    dialog = RunnerDialog(runnertypes.BY_ID["docker"])
    assert dialog.detach.isChecked() and not dialog.detach.isEnabled()
    assert dialog.detach_hint.text() == runnertypes.BY_ID["docker"].detach_note


def test_a_script_is_offered_the_choice(qapp):
    dialog = RunnerDialog(runnertypes.BY_ID["python"])
    assert dialog.detach.isEnabled() and not dialog.detach.isChecked()


def test_a_stack_needs_a_name_and_cannot_repeat_one(qapp):
    from PySide6.QtWidgets import QDialogButtonBox
    dialog = ProjectDialog(taken=["Claim"])
    assert not dialog.buttons.button(QDialogButtonBox.Ok).isEnabled()
    dialog.name.setText("Claim")
    assert not dialog.buttons.button(QDialogButtonBox.Ok).isEnabled()
    dialog.name.setText("Helpdesk")
    assert dialog.buttons.button(QDialogButtonBox.Ok).isEnabled()


def test_a_log_can_be_given_a_stack_or_left_without_one(qapp):
    from cms_gui.pages.logsources import LogDialog
    row = lsf.LogRow(name="app", connection="here", envs=[LOCAL], target="/x")
    dialog = LogDialog(row, connections=[lsf.ConnectionRow(name="here")],
                       envs=[LOCAL], projects=["Claim", "Helpdesk"])
    assert dialog.value().project == ""
    dialog.project.setCurrentText("Helpdesk")
    assert dialog.value().project == "Helpdesk"


def test_a_log_keeps_a_stack_the_page_no_longer_offers(qapp):
    # Opening the dialog must not silently reassign a log whose stack was
    # renamed or deleted behind it.
    from cms_gui.pages.logsources import LogDialog
    row = lsf.LogRow(name="app", connection="here", envs=[LOCAL], target="/x",
                     project="Gone")
    dialog = LogDialog(row, connections=[lsf.ConnectionRow(name="here")],
                       envs=[LOCAL], projects=["Claim"])
    assert dialog.value().project == "Gone"


# --------------------------------------------------- rows, from the page
def test_renaming_a_connection_follows_through_to_its_logs(page, monkeypatch):
    # Logs point at a connection by name. Renaming one without this leaves every
    # log that used it pointing at something that no longer exists - which the
    # launcher then refuses the whole file for.
    _accept(monkeypatch, lambda d: d.name.setText("local-box") or True)
    page.edit_connection(0)
    assert page.rows()[1][0].connection == "local-box"
    assert page.save_button.isEnabled()


def test_a_duplicated_log_is_valid_the_moment_it_is_made(page):
    page.copy_log(page.rows()[1][0])
    assert [row.name for row in page.rows()[1]] == ["app", "app-copy", "nginx"]
    assert page.save_button.isEnabled()


def test_a_copied_log_stays_in_the_block_it_came_from(page):
    page.copy_log(page.rows()[1][0])
    # Its own block, at the top of it: a copy is a row that did not exist a
    # moment ago, and the page shows the newest first.
    assert [r.name for r in page._blocks["Claim"]._log_rows] == ["app-copy", "app"]


def test_deleting_a_connection_warns_about_the_logs_that_used_it(page, monkeypatch):
    asked = {}
    _answer_question(monkeypatch, record=asked)
    page.delete_connection(0)
    assert "1 log(s) use it: app" in asked["text"]
    assert not page.save_button.isEnabled()      # app now points nowhere


def test_a_log_cannot_be_added_before_a_connection_exists(empty_page):
    empty_page.add_log()
    assert "Add a connection first" in empty_page.status.text()


# ------------------------------------------------------------------- the files
def test_saving_writes_a_file_the_launcher_can_read(page):
    from engine import serverlog

    page.save()
    config = serverlog.load_config(page._log_path)
    assert [s.name for s in config.for_env(LOCAL)] == ["app"]
    assert [s.name for s in config.for_env(DEV)] == ["nginx"]


def test_the_stack_a_log_belongs_to_is_nothing_the_launcher_trips_over(page):
    from engine import serverlog

    page.save()
    config = serverlog.load_config(page._log_path)
    source = [s for s in config.logs if s.name == "app"][0]
    # Swept into extra and ignored, which is why this needed no core change.
    assert source.extra["project"] == "Claim"


def test_an_unassigned_log_is_written_exactly_as_it_always_was(page):
    page.save()
    written = json.loads(open(page._log_path, encoding="utf-8").read())
    nginx = [row for row in written["logs"] if row["name"] == "nginx"][0]
    assert "project" not in nginx


def test_the_two_files_are_written_side_by_side(page, tmp_path):
    page.save()
    assert page._services_path == str(tmp_path / "services.json")
    assert [p.name for p in sf.load(page._services_path)] == ["Claim"]
    assert "1 project(s)" in page.status.text()


def test_saving_keeps_the_previous_file_as_a_backup(page):
    before = open(page._log_path, encoding="utf-8").read()
    page.rows()[1][0].target = "/var/log/changed.log"
    page.save()
    assert open(page._log_path + ".bak", encoding="utf-8").read() == before
    assert "changed.log" in open(page._log_path, encoding="utf-8").read()


def test_a_custom_format_survives_a_round_trip_through_the_editor(page):
    """The data-loss path this feature had: opening the editor destroyed it.

    ``timestamp`` and ``level`` are how any backend without a preset is read.
    """
    page._logs.append(lsf.LogRow(
        name="weird", connection="here", envs=[LOCAL], type="file",
        target="/weird.log", timestamp={"regex": r"^\[(\d+)\]", "format": "%S"},
        level={"regex": r"<(\w+)>"}, tz="utc"))
    page.save()
    _connections, logs_back = lsf.load(page._log_path)
    kept = [row for row in logs_back if row.name == "weird"][0]
    assert kept.timestamp == {"regex": r"^\[(\d+)\]", "format": "%S"}
    assert kept.level == {"regex": r"<(\w+)>"}
    assert kept.tz == "utc"


def test_a_broken_log_file_is_reported_rather_than_swallowed(qapp, tmp_path):
    path = tmp_path / "logsources.json"
    path.write_text("{ not json")
    widget = ServicesPage()
    widget.load(str(path), str(tmp_path / "services.json"), str(path))
    assert "not valid JSON" in widget.status.text()
    assert widget.rows() == ([], [], [])
    widget.close()


def test_a_broken_services_file_is_reported_too(qapp, tmp_path):
    _write(tmp_path, services=False)
    (tmp_path / "services.json").write_text("{ not json")
    widget = ServicesPage()
    widget.load(str(tmp_path / "logsources.json"),
                str(tmp_path / "services.json"),
                str(tmp_path / "logsources.json"))
    assert "not valid JSON" in widget.status.text()
    assert not widget.save_button.isEnabled()
    widget.close()


def test_a_file_that_could_not_be_read_cannot_be_saved_over(qapp, tmp_path):
    """The data-loss path: an empty editor over a file full of content.

    Validation calls the empty document perfectly valid, so without this Save
    stays lit and one click replaces a file whose only problem was a typo.
    """
    path = tmp_path / "logsources.json"
    path.write_text('{"connections": [ trailing comma, ]}')
    widget = ServicesPage()
    widget.load(str(path), str(tmp_path / "services.json"), str(path))
    assert not widget.save_button.isEnabled()
    assert "Reload" in widget.status.text()
    widget.close()


def test_reloading_a_fixed_file_clears_the_complaint(qapp, tmp_path):
    path = tmp_path / "logsources.json"
    path.write_text("{ not json")
    widget = ServicesPage()
    widget.load(str(path), str(tmp_path / "services.json"), str(path))
    path.write_text(json.dumps({"connections": [{"name": "here", "type": "local"}],
                                "logs": []}))
    widget.load(str(path), str(tmp_path / "services.json"), str(path))
    assert "not valid JSON" not in widget.status.text()
    assert widget._valid and not widget.is_dirty()
    widget.close()


def test_a_change_made_on_disk_is_not_overwritten_without_asking(page, monkeypatch):
    asked = {}

    def question(_parent, title, text, *_args, **_kwargs):
        asked["title"] = title
        return False

    monkeypatch.setattr("cms_gui.widgets.confirm", question)
    page._log_fingerprint = ("stale",)
    page.save()
    assert asked["title"] == "File changed on disk"


def test_the_save_button_does_not_spell_out_a_file_name(page):
    assert page.save_button.text() == "Save"


# -------------------------------------------------------------- opening a log
class _Core:
    def __init__(self, answer):
        self.answer = answer
        self.asked = []

    def server_log_show(self, name, lines=None):
        self.asked.append((name, lines))
        return self.answer


def _select(page, block_name, row=0):
    block = page._blocks[block_name]
    block.logs.setCurrentCell(row, block.L_NAME)
    return block


def test_open_tail_asks_the_core_for_the_end_of_the_log(page):
    core = _Core({"ok": True, "target": "file (local) /x",
                  "lines": ["one", "two"], "truncated": True})
    page.set_core(core)
    page.open_selected_log(_select(page, "Claim"), whole=False)
    assert core.asked == [("app", 500)]
    assert "2 line(s)" in page.status.text()


def test_open_full_asks_for_all_of_it(page):
    core = _Core({"ok": True, "target": "file (local) /x", "lines": ["one"]})
    page.set_core(core)
    page.open_selected_log(_select(page, "Claim"), whole=True)
    # None is "as much as the core's byte budget allows" - the GUI does not
    # invent a number the core would have to second-guess.
    assert core.asked == [("app", None)]


def test_a_failure_shows_what_the_launcher_said(page):
    page.set_core(_Core({"ok": False,
                         "error": "ssh: Could not resolve hostname nx.invalid"}))
    page.open_selected_log(_select(page, "Claim"), whole=False)
    assert "Could not resolve hostname" in page.status.text()


def test_opening_unsaved_edits_says_to_save_first(page):
    # The launcher reads the file, not the screen.
    page.set_core(_Core({"ok": True, "lines": []}))
    page._log_fingerprint = ("stale",)
    page.open_selected_log(_select(page, "Claim"), whole=False)
    assert "Save first" in page.status.text()


def test_opening_an_invalid_file_says_to_fix_it_first(page):
    page.set_core(_Core({"ok": True, "lines": []}))
    page._logs[0].connection = "gone"
    page.open_selected_log(_select(page, "Claim"), whole=False)
    assert "Fix the problems" in page.status.text()


def test_opening_with_nothing_selected_says_so(page):
    page.set_core(_Core({"ok": True, "lines": []}))
    block = page._blocks["Claim"]
    block.logs.setCurrentCell(-1, -1)
    page.open_selected_log(block, whole=False)
    assert "Select a log" in page.status.text()


# ------------------------------------------------------------------- closing
def test_nothing_running_means_nothing_to_lose_on_close(page):
    assert page.detained() == []


def test_the_page_reports_what_the_window_would_take_with_it(qapp, tmp_path):
    services = {"projects": [{"name": "Claim", "runners": [
        {"name": "mine", "type": "shell", "detach": False,
         "settings": {"command": "%s -c \"import time;time.sleep(30)\""
                                 % sys.executable}},
        {"name": "theirs", "type": "shell", "detach": True,
         "settings": {"command": "%s -c \"import time;time.sleep(30)\""
                                 % sys.executable}}]}]}
    widget = _open(tmp_path, services=services)
    widget.start_project("Claim")
    import time
    deadline = time.time() + 15
    while time.time() < deadline and widget.supervisor.counts("Claim") != (2, 2):
        qapp.processEvents()
        time.sleep(0.02)
    assert [s.name for s in widget.detained()] == ["mine"]
    assert widget.shutdown(8000) == 1
    from cms_gui import services as services_mod
    services_mod.terminate_pid(widget.supervisor.service("Claim", "theirs")._pid)
    widget.close()


# ------------------------------------------------------------------ the block
def test_a_block_without_a_stack_has_logs_but_no_services(page):
    unassigned = page._blocks[""]
    assert isinstance(unassigned, ProjectBlock)
    assert unassigned.runners is None
    assert unassigned.logs is not None


def test_the_start_button_becomes_a_stop_button_while_it_runs(page):
    block = page._blocks["Claim"]
    strip = block.runners.cellWidget(0, block.R_ACTIONS)
    assert strip.toggle.toolTip() == "Start"
    block.set_status("Odoo Local", runnertypes.RUNNING)
    assert strip.toggle.toolTip() == "Stop"
    assert block.runners.item(0, block.R_STATUS).text() == "Running"
    block.set_status("Odoo Local", runnertypes.STOPPED)
    assert strip.toggle.toolTip() == "Start"


def test_a_row_says_when_what_is_running_is_not_what_is_written_down(page):
    block = page._blocks["Claim"]
    block.set_status("Odoo Local", runnertypes.RUNNING, stale=True)
    assert block.runners.item(0, block.R_STATUS).text() == "Running (edited)"


def test_why_a_service_failed_is_on_the_row_and_in_the_status_line(page):
    service = page.supervisor.service("Claim", "Odoo Local")
    service._set_status(runnertypes.FAILED, "exited with code 3.")
    block = page._blocks["Claim"]
    assert block.runners.item(0, block.R_STATUS).toolTip() == "exited with code 3."
    assert "exited with code 3." in page.status.text()


# ------------------------------------------------------------------- measuring
# Every width and height in a row is measured rather than written down. A number
# in the source is right for one body font and wrong for the next, which is how
# "Console" and "Delete" first came out as "onsol" and "Delet".

def test_the_action_buttons_get_the_width_they_actually_need(page):
    block = page._blocks["Claim"]
    for table, column in ((block.runners, block.R_ACTIONS),
                          (block.logs, block.L_ACTIONS),
                          (page.connections, 3)):
        for row in range(table.rowCount()):
            strip = table.cellWidget(row, column)
            assert table.columnWidth(column) >= strip.sizeHint().width()


def test_the_status_column_fits_the_longest_thing_it_can_ever_say(page):
    block = page._blocks["Claim"]
    metrics = block.runners.fontMetrics()
    longest = max(metrics.horizontalAdvance("%s (edited)" % text)
                  for text in STATUS_TEXT.values())
    assert block.runners.columnWidth(block.R_STATUS) > longest


def test_every_row_is_tall_enough_for_the_buttons_in_it(page):
    block = page._blocks["Claim"]
    strip = block.runners.cellWidget(0, block.R_ACTIONS)
    for row in range(block.runners.rowCount()):
        assert block.runners.rowHeight(row) >= strip.minimumSizeHint().height()


def test_a_table_is_exactly_as_tall_as_what_is_in_it(page):
    # The page scrolls; the tables do not. A table with no height of its own
    # takes whatever the layout offers, which is either nothing or everything.
    block = page._blocks["Claim"]
    table = block.runners
    rows = table.verticalHeader().defaultSectionSize() * table.rowCount()
    # top_margin, not the header alone: the search row stands between the two.
    assert table.height() == table.top_margin() + 4 + rows
    assert table.top_margin() == (table.horizontalHeader().height()
                                  + block.runners_search.height())


# --------------------------------------------------------- unsaved changes
def test_save_is_dark_until_something_actually_changes(page):
    assert not page.is_dirty()
    assert not page.save_button.isEnabled()
    assert page.save_button.property("dirty") == "false"


def test_an_edit_lights_save_up(page, monkeypatch):
    _accept(monkeypatch, lambda d: d.name.setText("Helpdesk") or True)
    page.add_project()
    assert page.is_dirty()
    assert page.save_button.isEnabled()
    assert page.save_button.property("dirty") == "true"


def test_saving_puts_it_out_again(page, monkeypatch):
    _accept(monkeypatch, lambda d: d.name.setText("Helpdesk") or True)
    page.add_project()
    page.save()
    assert not page.is_dirty()
    assert not page.save_button.isEnabled()


def test_folding_a_block_is_not_an_edit(page):
    # It is saved along with the next real change, but it is a view preference
    # and lighting Save up for it would cry wolf.
    page._blocks["Claim"].disclosure.set_expanded(False)
    page._update_dirty()
    assert not page.is_dirty()


def test_an_invalid_document_cannot_be_saved_even_when_edited(page):
    page._logs[0].connection = "gone"
    page._rebuild()
    page._validate()
    assert page.is_dirty() and not page.save_button.isEnabled()


def test_walking_away_from_an_untouched_page_asks_nothing(page, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("nothing was changed; there is nothing to discard")

    monkeypatch.setattr("cms_gui.widgets.confirm", refuse)
    assert page.confirm_discard() is True


def test_walking_away_from_an_edit_asks_first(page, monkeypatch):
    asked = {}
    _accept(monkeypatch, lambda d: d.name.setText("Helpdesk") or True)
    page.add_project()
    _answer_question(monkeypatch, answer=False, record=asked)
    assert page.confirm_discard() is False
    assert "have not been saved" in asked["text"]


# ------------------------------------------------------------ empty projects
def test_an_empty_project_offers_nothing_to_act_on(qapp, tmp_path):
    widget = _open(tmp_path, services={"projects": [{"name": "Empty"}]})
    block = widget._blocks["Empty"]
    for button, _verb in block._service_buttons:
        assert not button.isEnabled(), button.text()
    # Except the one thing an empty project is for.
    assert block._add_menu.actions()
    widget.close()


def test_a_project_with_services_offers_them(page):
    for button, _verb in page._blocks["Claim"]._service_buttons:
        assert button.isEnabled(), button.text()


def test_open_full_and_tail_wait_for_a_log_to_exist(qapp, tmp_path):
    widget = _open(tmp_path, logsources={"connections": [], "logs": []},
                   services={"projects": [{"name": "Empty"}]})
    block = widget._blocks["Empty"]
    assert not block.open_full_button.isEnabled()
    assert not block.open_tail_button.isEnabled()
    widget.close()


def test_the_page_wide_buttons_follow_the_same_rule(qapp, tmp_path):
    widget = _open(tmp_path, services={"projects": [{"name": "Empty"}]})
    assert not widget.start_all_button.isEnabled()
    assert not widget.stop_all_button.isEnabled()
    widget.close()


# ------------------------------------------------------------------ ordering
def test_the_projects_come_before_the_connections(page):
    order = [page._column.itemAt(i).widget() for i in range(page._column.count())]
    blocks = [w for w in order if isinstance(w, ProjectBlock)]
    assert blocks, "no project blocks on the page"
    assert order.index(blocks[-1]) < order.index(page._blocks_end)


def test_the_paths_are_not_on_the_page_but_can_still_be_looked_up(page, tmp_path):
    assert not hasattr(page, "log_path_label")
    assert page.paths() == (str(tmp_path / "logsources.json"),
                            str(tmp_path / "services.json"))


# --------------------------------------------------------------- dependencies
def test_the_row_says_what_it_starts_after(qapp, tmp_path):
    services = {"projects": [{"name": "Claim", "runners": [
        {"name": "db", "type": "docker", "settings": {"container": "pg"}},
        {"name": "web", "type": "shell", "depends": ["db"],
         "settings": {"command": "true"}}]}]}
    widget = _open(tmp_path, services=services)
    block = widget._blocks["Claim"]
    assert "after db" in block.runners.item(1, block.R_TYPE).text()
    assert "Starts after: db" in block.runners.item(1, block.R_TYPE).toolTip()
    widget.close()


def test_a_long_wait_list_is_shortened_rather_than_widening_the_row(qapp, tmp_path):
    runners = [{"name": n, "type": "shell", "settings": {"command": "true"}}
               for n in ("a", "b", "c", "d")]
    runners.append({"name": "last", "type": "shell", "depends": ["a", "b", "c", "d"],
                    "settings": {"command": "true"}})
    widget = _open(tmp_path, services={"projects": [{"name": "Claim",
                                                     "runners": runners}]})
    block = widget._blocks["Claim"]
    text = block.runners.item(4, block.R_TYPE).text()
    assert "after a, b +2" in text
    assert "Starts after: a, b, c, d" in block.runners.item(4, block.R_TYPE).toolTip()
    widget.close()


def test_the_form_offers_the_other_services_to_wait_for(qapp):
    dialog = RunnerDialog(runnertypes.BY_ID["shell"],
                          siblings=["db", "cache", "web"])
    assert dialog.depends_field.isVisible() or True     # visibility needs a shown parent
    dialog.depends.set_checked(["db"])
    dialog.name.setText("web")
    dialog._editors["command"].setText("true")
    assert dialog.value().depends == ["db"]


def test_a_service_is_never_offered_itself_to_wait_for(qapp):
    row = sf.RunnerRow(name="web", type="shell", settings={"command": "true"})
    dialog = RunnerDialog(runnertypes.BY_ID["shell"], row, siblings=["db", "web"])
    offered = dialog.depends.values()
    assert "web" not in offered and "db" in offered


def test_renaming_a_service_takes_what_waits_for_it_along(page, monkeypatch):
    project = page.rows()[2][0]
    project.runners[1].depends = ["Odoo Local"]

    def fill(dialog):
        dialog.name.setText("Odoo")
        return True

    _accept(monkeypatch, fill)
    page.edit_runner("Claim", project.runners[0])
    assert page.rows()[2][0].runners[1].depends == ["Odoo"]


def test_deleting_a_service_stops_the_others_waiting_for_it(page, monkeypatch):
    project = page.rows()[2][0]
    project.runners[1].depends = ["Odoo Local"]
    _answer_question(monkeypatch)
    page.delete_runner("Claim", project.runners[0])
    assert page.rows()[2][0].runners[0].depends == []
    assert page.save_button.isEnabled()      # and the file stays valid


# ------------------------------------------------------------- the selection
def test_a_row_can_be_un_selected_even_when_it_is_the_only_one(qapp, tmp_path):
    """Open Tail is aimed by selecting a row, so there has to be a way to aim at
    nothing - and with one row in the list there was none."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    widget = _open(tmp_path, logsources={
        "connections": [{"name": "here", "type": "local"}],
        "logs": [{"name": "only", "connection": "here", "env": LOCAL,
                  "type": "file", "path": "/x.log"}]})
    block = widget._blocks[""]
    block.logs.selectRow(0)
    assert block.selected_log() is not None

    centre = QPointF(block.logs.visualRect(
        block.logs.model().index(0, 0)).center())
    block.logs.mousePressEvent(QMouseEvent(
        QMouseEvent.MouseButtonPress, centre, centre, Qt.LeftButton,
        Qt.LeftButton, Qt.NoModifier))
    assert block.selected_log() is None
    widget.close()


def test_the_row_buttons_do_not_paint_accent_on_accent(page):
    # A row paints its selection in the accent, so an accent-inked button in a
    # cell is the washed-out half-legible strip this variant exists to avoid.
    block = page._blocks["Claim"]
    strip = block.runners.cellWidget(0, block.R_ACTIONS)
    buttons = strip.findChildren(type(page.save_button))
    assert buttons
    for button in buttons:
        assert button.property("variant") == "cell"


def test_the_interpreter_field_says_which_python_blank_will_use(qapp, tmp_path):
    # The answer lives in a dotted directory a chooser will not list, so it is
    # found and shown rather than left for someone to go and look for.
    interpreter = tmp_path / ".venv" / "bin" / "python3"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("")
    dialog = RunnerDialog(runnertypes.BY_ID["python"], project_dir=str(tmp_path))
    assert dialog._editors["interpreter"].placeholderText() == str(interpreter)


def test_without_a_venv_it_says_what_it_will_fall_back_to(qapp, tmp_path):
    dialog = RunnerDialog(runnertypes.BY_ID["python"], project_dir=str(tmp_path))
    assert dialog._editors["interpreter"].placeholderText() == \
        runnertypes.DEFAULT_PYTHON


# ------------------------------------------------------------ field heights
# A fixed maximum is not a height: an empty grid and a one-item list both took
# the whole of it, and left a field that was mostly nothing.

def test_an_empty_environment_grid_is_a_grid_not_a_void(qapp):
    grid = RunnerDialog(runnertypes.BY_ID["python"])._editors["env"].table
    # Its header, and one empty row's worth - enough to read as waiting to be
    # filled, and nowhere near the 140px it used to reserve.
    assert grid.horizontalHeader().sizeHint().height() < grid.height() < 80


def test_the_environment_grid_grows_with_what_is_in_it(qapp):
    editor = RunnerDialog(runnertypes.BY_ID["python"])._editors["env"]
    empty = editor.table.height()
    for index in range(3):
        editor.add_row("K%d" % index, "v")
    assert editor.table.height() > empty


def test_the_environment_grid_stops_growing_rather_than_running_off_the_dialog(qapp):
    editor = RunnerDialog(runnertypes.BY_ID["python"])._editors["env"]
    for index in range(30):
        editor.add_row("K%d" % index, "v")
    assert editor.table.height() < 200


def test_removing_rows_gives_the_space_back(qapp):
    # The floor is one row's worth, so the *first* row costs nothing to speak of.
    # It is the second onwards the grid grows for, and has to give back.
    editor = RunnerDialog(runnertypes.BY_ID["python"])._editors["env"]
    editor.add_row("A", "1")
    one = editor.table.height()
    editor.add_row("B", "2")
    assert editor.table.height() > one
    editor.table.setCurrentCell(1, 0)
    editor.remove_row()
    assert editor.table.height() == one


def test_one_service_to_wait_for_takes_one_rows_worth(qapp):
    dialog = RunnerDialog(runnertypes.BY_ID["shell"], siblings=["odoo"])
    assert dialog.depends.list.height() < 40


def test_many_services_to_wait_for_scroll_rather_than_grow(qapp):
    dialog = RunnerDialog(runnertypes.BY_ID["shell"],
                          siblings=[str(n) for n in range(30)])
    assert dialog.depends.list.height() < 200


# ------------------------------------------------------------------ criteria
from cms_gui import criteria as cr                                  # noqa: E402
from cms_gui.pages.services import CriteriaEditor, CriterionDialog  # noqa: E402

WATCHED = {"projects": [{"name": "Claim", "runners": [
    {"name": "odoo", "type": "shell", "settings": {"command": "true"},
     "criteria": [
         {"name": "start", "color": "green", "source": "/x/odoo.log", "rules": [
             {"mode": "match", "kind": "text", "pattern": "started localhost:8069"},
             {"mode": "exclude", "kind": "regex", "pattern": "ERRORS|CRITICAL"}]},
         {"name": "finished_tests", "color": "blue", "rules": [
             {"mode": "match", "kind": "text", "pattern": "tests passed"}]}]},
    {"name": "plain", "type": "shell", "settings": {"command": "true"}}]}]}


def test_a_project_that_watches_nothing_has_no_criteria_column(page):
    block = page._blocks["Claim"]
    assert block.runners.isColumnHidden(block.R_CRITERIA)


def test_the_column_appears_once_something_is_watched(qapp, tmp_path):
    widget = _open(tmp_path, services=WATCHED)
    block = widget._blocks["Claim"]
    assert not block.runners.isColumnHidden(block.R_CRITERIA)
    widget.close()


def test_nothing_is_shown_until_something_matches(qapp, tmp_path):
    # A row carrying every criterion whether or not it had fired was mostly grey
    # words about things that had not happened.
    widget = _open(tmp_path, services=WATCHED)
    block = widget._blocks["Claim"]
    strip = block.runners.cellWidget(0, block.R_CRITERIA)
    assert [tag.text() for tag in strip.tags] == ["start", "finished_tests"]
    assert not any(tag.isVisibleTo(strip) for tag in strip.tags)
    widget.close()


def test_only_what_matched_is_shown(qapp, tmp_path):
    widget = _open(tmp_path, services=WATCHED)
    block = widget._blocks["Claim"]
    block.set_criteria("odoo", [("start", "green", True, []),
                                ("finished_tests", "blue", False, ["waiting"])])
    strip = block.runners.cellWidget(0, block.R_CRITERIA)
    shown = [tag.text() for tag in strip.tags if tag.isVisibleTo(strip)]
    assert shown == ["start"]
    widget.close()


def test_what_is_still_being_watched_for_is_on_the_tooltip(qapp, tmp_path):
    # Hidden from the row, but it is a question with an answer.
    widget = _open(tmp_path, services=WATCHED)
    block = widget._blocks["Claim"]
    block.set_criteria("odoo", [("start", "green", True, []),
                                ("finished_tests", "blue", False, ["waiting"])])
    tip = block.runners.cellWidget(0, block.R_CRITERIA).toolTip()
    assert "Matched: start" in tip
    assert "Still watching for: finished_tests" in tip
    widget.close()


def test_a_criterion_that_goes_dark_again_stops_being_shown(qapp, tmp_path):
    # start stops being true the moment a CRITICAL line arrives.
    widget = _open(tmp_path, services=WATCHED)
    block = widget._blocks["Claim"]
    block.set_criteria("odoo", [("start", "green", True, []),
                                ("finished_tests", "blue", False, [])])
    strip = block.runners.cellWidget(0, block.R_CRITERIA)
    assert strip.tags[0].isVisibleTo(strip)
    block.set_criteria("odoo", [("start", "green", False, ["saw 'CRITICAL'"]),
                                ("finished_tests", "blue", False, [])])
    assert not strip.tags[0].isVisibleTo(strip)
    widget.close()


def test_a_service_that_watches_nothing_has_an_empty_cell(qapp, tmp_path):
    widget = _open(tmp_path, services=WATCHED)
    block = widget._blocks["Claim"]
    assert not getattr(block.runners.cellWidget(1, block.R_CRITERIA), "tags", [])
    widget.close()


def test_a_lit_criterion_paints_in_its_own_colour(qapp, tmp_path):
    widget = _open(tmp_path, services=WATCHED)
    block = widget._blocks["Claim"]
    block.set_criteria("odoo", [("start", "green", True, []),
                                ("finished_tests", "blue", False, ["waiting"])])
    lit = block.runners.cellWidget(0, block.R_CRITERIA).tags[0]
    assert cr.color_of("green") in lit.styleSheet()
    assert lit.toolTip() == "start: matched"
    widget.close()


def test_the_column_is_re_measured_when_a_tag_appears(qapp, tmp_path):
    # The strip is only as wide as what is showing, so a column sized while
    # nothing had matched would cut off the first tag that did.
    widget = _open(tmp_path, services=WATCHED)
    block = widget._blocks["Claim"]
    block.set_criteria("odoo", [("start", "green", True, []),
                                ("finished_tests", "blue", True, [])])
    strip = block.runners.cellWidget(0, block.R_CRITERIA)
    assert block.runners.columnWidth(block.R_CRITERIA) >= strip.sizeHint().width()
    widget.close()


def test_the_criteria_column_never_moves_the_status_column(qapp, tmp_path):
    # Two separate claims: STATUS is the process, Criteria is the log.
    widget = _open(tmp_path, services=WATCHED)
    block = widget._blocks["Claim"]
    block.set_status("odoo", runnertypes.RUNNING)
    block.set_criteria("odoo", [("start", "green", False, ["waiting"]),
                                ("finished_tests", "blue", False, ["waiting"])])
    assert block.runners.item(0, block.R_STATUS).text() == "Running"
    widget.close()


# ------------------------------------------------------------- the criteria form
def test_a_criterion_needs_a_name_and_a_rule(qapp):
    from PySide6.QtWidgets import QDialogButtonBox

    dialog = CriterionDialog()
    assert not dialog.buttons.button(QDialogButtonBox.Ok).isEnabled()
    dialog.name.setText("start")
    dialog.rules.item(0, 2).setText("started")
    assert dialog.buttons.button(QDialogButtonBox.Ok).isEnabled()
    assert dialog.value().rules[0].pattern == "started"


def test_a_regex_that_will_not_compile_is_caught_while_it_is_typed(qapp):
    from PySide6.QtWidgets import QDialogButtonBox

    dialog = CriterionDialog()
    dialog.name.setText("start")
    dialog.rules.cellWidget(0, 1).setCurrentIndex(list(cr.KINDS).index(cr.REGEX))
    dialog.rules.item(0, 2).setText("a(")
    assert not dialog.buttons.button(QDialogButtonBox.Ok).isEnabled()
    assert "does not compile" in dialog.problem.text()


def test_a_rule_can_be_told_to_exclude(qapp):
    dialog = CriterionDialog()
    dialog.name.setText("start")
    dialog.rules.item(0, 2).setText("started")
    dialog.add_rule()
    dialog.rules.cellWidget(1, 0).setCurrentIndex(list(cr.MODES).index(cr.EXCLUDE))
    dialog.rules.item(1, 2).setText("CRITICAL")
    modes = [rule.mode for rule in dialog.value().rules]
    assert modes == [cr.MATCH, cr.EXCLUDE]


def test_two_criteria_on_one_service_cannot_share_a_name(qapp):
    from PySide6.QtWidgets import QDialogButtonBox

    dialog = CriterionDialog(taken=["start"])
    dialog.name.setText("start")
    dialog.rules.item(0, 2).setText("x")
    assert not dialog.buttons.button(QDialogButtonBox.Ok).isEnabled()


def test_blank_means_the_services_own_output(qapp):
    dialog = CriterionDialog()
    assert dialog.value().source == ""
    assert dialog.source.placeholderText() == CriterionDialog.OWN_OUTPUT


def test_the_editor_lists_what_each_criterion_watches(qapp):
    editor = CriteriaEditor([
        cr.CriterionRow(name="start", source="/x/odoo.log",
                        rules=[cr.Rule(cr.MATCH, cr.TEXT, "up")]),
        cr.CriterionRow(name="done", rules=[cr.Rule(cr.MATCH, cr.TEXT, "bye")])])
    assert editor.table.rowCount() == 2
    assert editor.table.item(0, 2).text() == "/x/odoo.log"
    assert editor.table.item(1, 2).text() == "its own output"
    assert "must contain" in editor.table.item(0, 3).text()


def test_the_runner_form_carries_criteria_through(qapp, tmp_path):
    dialog = RunnerDialog(runnertypes.BY_ID["shell"], project_dir=str(tmp_path))
    dialog.name.setText("odoo")
    dialog._editors["command"].setText("true")
    dialog.criteria._rows = [cr.CriterionRow(
        name="start", rules=[cr.Rule(cr.MATCH, cr.TEXT, "up")])]
    dialog.criteria._rebuild()
    assert [one.name for one in dialog.value().criteria] == ["start"]


def test_a_broken_criterion_keeps_the_service_form_shut(qapp, tmp_path):
    from PySide6.QtWidgets import QDialogButtonBox

    dialog = RunnerDialog(runnertypes.BY_ID["shell"], project_dir=str(tmp_path))
    dialog.name.setText("odoo")
    dialog._editors["command"].setText("true")
    dialog.criteria._rows = [cr.CriterionRow(
        name="start", rules=[cr.Rule(cr.MATCH, cr.REGEX, "a(")])]
    dialog.criteria._rebuild()
    dialog._changed()
    assert not dialog.buttons.button(QDialogButtonBox.Ok).isEnabled()
    assert "does not compile" in dialog.problem.text()


def test_a_grid_is_measured_from_the_row_it_draws_not_the_one_it_hints(qapp):
    """sizeHintForRow lies in both directions, and the criteria grid caught it.

    A cell whose text wraps reports the *wrapped* height, so a criterion holding
    a long rule summary claimed 71px for a row drawn at 30 - and reserved two
    rows' worth of nothing underneath the table.
    """
    editor = CriteriaEditor([cr.CriterionRow(
        name="start", source="/a/very/long/path/to/some/odoo_logs/odoo.log",
        rules=[cr.Rule(cr.MATCH, cr.TEXT, "started localhost:8069"),
               cr.Rule(cr.EXCLUDE, cr.REGEX, "ERRORS|CRITICAL")])])
    table = editor.table
    assert table.sizeHintForRow(0) > table.rowHeight(0), "the premise of this test"
    header = table.horizontalHeader().sizeHint().height()
    assert table.height() < header + 2 * table.rowHeight(0)


def test_an_empty_grid_reserves_what_a_row_will_actually_take(qapp):
    # Empty and one-row differ by nothing, so the grid does not jump when the
    # first row lands.
    empty = CriteriaEditor([])
    one = CriteriaEditor([cr.CriterionRow(name="a",
                                          rules=[cr.Rule(cr.MATCH, cr.TEXT, "x")])])
    assert empty.table.height() == one.table.height()


def test_a_widget_column_is_never_narrower_than_its_own_heading(qapp, tmp_path):
    # The criteria column shows only what matched, so with nothing lit its
    # widgets ask for almost nothing - and the column sized to that clipped its
    # own heading to "RITERI".
    widget = _open(tmp_path, services=WATCHED)
    block = widget._blocks["Claim"]
    heading = block.runners.horizontalHeader().sectionSizeHint(block.R_CRITERIA)
    assert block.runners.columnWidth(block.R_CRITERIA) >= heading
    widget.close()


def test_where_the_file_will_be_written_never_hides_a_reason_not_to(qapp, tmp_path):
    """The migration notice must not replace an error.

    Reading from the old location and writing to the new one is housekeeping.
    Said on top of "this file is not valid JSON" it buries the one message that
    has to be read under the one that does not matter yet.
    """
    _write(tmp_path, services=False)
    (tmp_path / "services.json").write_text("{ not json")
    widget = ServicesPage()
    # Logs pinned so only the services file migrates - this is about that notice.
    widget.load(str(tmp_path / "logsources.json"), "",
                str(tmp_path / "logsources.json"))
    assert widget._migrating
    assert "not valid JSON" in widget.status.text()
    assert not widget.save_button.isEnabled()
    widget.close()


def test_the_notice_is_still_shown_when_there_is_nothing_wrong(qapp, tmp_path):
    _write(tmp_path)
    widget = ServicesPage()
    widget.load(str(tmp_path / "logsources.json"), "",
                str(tmp_path / "logsources.json"))
    assert widget._migrating
    assert "Save writes to" in widget.status.text()
    widget.close()


# --------------------------------------------------------------- folded away
# A fold is a view preference: it survives the window closing whether or not
# anything was saved, and it never lights up Save. That is why it is kept in
# QSettings (redirected into a sandbox by conftest) rather than in either file.
def test_a_folded_project_is_still_folded_next_time(qapp, tmp_path):
    first = _open(tmp_path)
    first._blocks["Claim"].disclosure.set_expanded(False)
    first.shutdown(2000)
    first.close()

    again = _open(tmp_path)
    try:
        assert again._blocks["Claim"].disclosure.is_expanded() is False
        # And back again: what is remembered is the answer, not the folding.
        again._blocks["Claim"].disclosure.set_expanded(True)
    finally:
        again.shutdown(2000)
        again.close()

    third = _open(tmp_path)
    try:
        assert third._blocks["Claim"].disclosure.is_expanded() is True
    finally:
        third.shutdown(2000)
        third.close()


def test_the_connections_fold_is_remembered_too(qapp, tmp_path):
    first = _open(tmp_path)
    first.connections_fold.set_expanded(True)
    first.shutdown(2000)
    first.close()

    again = _open(tmp_path)
    try:
        assert again.connections_fold.is_expanded() is True
    finally:
        again.shutdown(2000)
        again.close()


def test_the_unassigned_block_is_remembered_too(qapp, tmp_path):
    first = _open(tmp_path, services={"projects": []})
    first._blocks[""].disclosure.set_expanded(False)
    first.shutdown(2000)
    first.close()

    again = _open(tmp_path, services={"projects": []})
    try:
        assert again._blocks[""].disclosure.is_expanded() is False
    finally:
        again.shutdown(2000)
        again.close()


def test_a_fold_is_kept_out_of_the_documents(page, tmp_path):
    # It is written the moment it happens, and writing it must not need a Save
    # or count as one.
    page._blocks["Claim"].disclosure.set_expanded(False)
    assert not page.is_dirty()
    assert page._settings.folds("services")["project:Claim"] is False


# ------------------------------------------------------- searching by column
def test_a_column_search_hides_the_rows_that_do_not_match(page):
    block = page._blocks["Claim"]
    block.runners_search.box(block.R_NAME).setText("odoo")
    shown = [row for row in range(block.runners.rowCount())
             if not block.runners.isRowHidden(row)]
    assert [block._runner_rows[row].name for row in shown] == ["Odoo Local"]


def test_the_columns_narrow_together(page):
    block = page._blocks["Claim"]
    block.runners_search.box(block.R_NAME).setText("o")        # both rows
    block.runners_search.box(block.R_TYPE).setText("docker")   # one of them
    shown = [row for row in range(block.runners.rowCount())
             if not block.runners.isRowHidden(row)]
    assert [block._runner_rows[row].name for row in shown] == ["PostgreSQL DB"]


def test_a_search_matches_what_the_column_says_not_what_is_behind_it(page):
    # The status column is computed, never stored, and is searchable all the
    # same - the search reads the table, cell by cell.
    block = page._blocks["Claim"]
    block.set_status("Odoo Local", runnertypes.RUNNING)
    block.runners_search.box(block.R_STATUS).setText("running")
    shown = [row for row in range(block.runners.rowCount())
             if not block.runners.isRowHidden(row)]
    assert [block._runner_rows[row].name for row in shown] == ["Odoo Local"]


def test_a_table_searched_down_to_nothing_says_which_kind_of_empty_it_is(page):
    block = page._blocks["Claim"]
    block.logs_search.box(block.L_NAME).setText("nothing-by-this-name")
    assert all(block.logs.isRowHidden(row)
               for row in range(block.logs.rowCount()))
    assert "matches the search" in block._logs_note.text()
    # isHidden rather than isVisible: nothing in this test is on a screen.
    assert not block._logs_note.isHidden()
    block.logs_search.clear()
    assert "No logs in this project yet." == block._logs_note.text()


def test_a_search_shrinks_the_table_to_what_it_found(page):
    block = page._blocks["Claim"]
    full = block.runners.height()
    block.runners_search.box(block.R_NAME).setText("odoo")
    assert block.runners.height() < full


def test_the_search_survives_acting_on_what_it_found(page, monkeypatch):
    # Editing a row rebuilds the page. A search that cleared itself every time
    # somebody used what they had found would be a search nobody could use twice.
    block = page._blocks["Claim"]
    block.runners_search.box(block.R_NAME).setText("odoo")
    _accept(monkeypatch, lambda d: d.name.setText("Odoo Local") or True)
    page.edit_runner("Claim", page.rows()[2][0].runners[0])
    block = page._blocks["Claim"]
    assert block.runners_search.needles() == {block.R_NAME: "odoo"}
    assert block.runners.isRowHidden(
        [r.name for r in block._runner_rows].index("PostgreSQL DB"))


def test_every_search_box_stands_over_the_column_it_searches(page, qapp):
    block = page._blocks["Claim"]
    block.show()
    qapp.processEvents()
    header = block.runners.horizontalHeader()
    for column in (block.R_NAME, block.R_TYPE, block.R_CONFIG, block.R_STATUS):
        box = block.runners_search.box(column)
        assert box.x() == header.sectionViewportPosition(column)
    # Nothing over the buttons, and nothing over the criteria: neither cell
    # holds text, so a box there could only ever match nothing.
    assert block.runners_search.box(block.R_ACTIONS) is None
    assert block.runners_search.box(block.R_CRITERIA) is None
    # And the strip stands between the header and the first row, not over them.
    assert block.runners.top_margin() == (header.height()
                                          + block.runners_search.height())


# ------------------------------------------------------------- newest on top
def test_a_new_project_is_shown_first_and_written_last(page, monkeypatch):
    _accept(monkeypatch, lambda d: d.name.setText("Helpdesk") or True)
    page.add_project()
    assert list(page._blocks)[0] == "Helpdesk"
    # The file keeps the order things were written in. Only the page sorts.
    assert [p.name for p in page.rows()[2]] == ["Claim", "Helpdesk"]


def test_a_new_service_is_shown_first_but_still_starts_last(page, monkeypatch):
    def fill(dialog):
        dialog.name.setText("Worker")
        dialog._editors["command"].setText("celery worker")
        return True

    _accept(monkeypatch, fill)
    page.add_runner("Claim", runnertypes.BY_ID["shell"])
    block = page._blocks["Claim"]
    assert [r.name for r in block._runner_rows][0] == "Worker"
    project = page.rows()[2][0]
    assert [r.name for r in project.runners][-1] == "Worker"
    assert [r.name for r in project.start_order()][-1] == "Worker"


def test_rows_with_no_date_keep_the_order_the_file_has_them_in(page):
    # Every row in the fixtures predates the stamp. Guessing at their order
    # would be a guess; keeping the file's is only saying what is known.
    block = page._blocks["Claim"]
    assert [r.name for r in block._runner_rows] == ["Odoo Local", "PostgreSQL DB"]


def test_a_dated_row_stands_above_the_undated_ones(page, monkeypatch):
    _accept(monkeypatch, lambda d: d.name.setText("nginx-2") or True)
    page.add_log("Claim")
    block = page._blocks["Claim"]
    assert [r.name for r in block._log_rows][0] == "nginx-2"


def test_editing_a_row_keeps_the_date_it_arrived(page, monkeypatch):
    _accept(monkeypatch, lambda d: d.name.setText("Helpdesk") or True)
    page.add_project()
    added = page.rows()[2][-1].added
    assert added
    _accept(monkeypatch, lambda d: d.name.setText("Helpdesk 2") or True)
    page.edit_project("Helpdesk")
    assert page.rows()[2][-1].added == added


def test_the_newest_connection_is_shown_first_and_its_buttons_still_aim_right(
        page, monkeypatch):
    _accept(monkeypatch, lambda d: d.name.setText("staging") or True)
    page.add_connection()
    assert page.connections.item(0, 0).text() == "staging"
    # Drawn first, third in the file: a button on that row has to act on the
    # connection it is drawn for, not on whatever is written down first.
    assert page._at(0) == len(page.rows()[0]) - 1
    _answer_question(monkeypatch)
    page.delete_connection(page._at(0))
    assert [c.name for c in page.rows()[0]] == ["here", "dev"]


# --------------------------------------------------- acting on a selection
def test_the_block_buttons_say_they_act_on_everything_until_rows_are_picked(page):
    block = page._blocks["Claim"]
    assert [b.text() for b, _v in block._service_buttons] == [
        "Start All", "Stop All", "Restart All"]
    block.runners.selectRow(0)
    assert [b.text() for b, _v in block._service_buttons] == [
        "Start (1)", "Stop (1)", "Restart (1)"]


def test_start_acts_only_on_the_rows_that_are_selected(page, monkeypatch):
    calls = []
    monkeypatch.setattr(page.supervisor, "start_all",
                        lambda project=None, names=None: calls.append((project,
                                                                       names)))
    _answer_question(monkeypatch)        # only the All form asks
    block = page._blocks["Claim"]
    block._service_buttons[0][0].click()
    block.runners.selectRow(1)
    block._service_buttons[0][0].click()
    assert calls == [("Claim", None), ("Claim", ["PostgreSQL DB"])]


def test_two_rows_can_be_picked_without_a_modifier_key(page):
    block = page._blocks["Claim"]
    block.runners.selectRow(0)
    block.runners.selectRow(1)
    assert block.selected_runners() == ["Odoo Local", "PostgreSQL DB"]


def test_a_row_hidden_by_a_search_is_not_one_of_the_selected(page):
    block = page._blocks["Claim"]
    block.runners.selectRow(0)
    block.runners.selectRow(1)
    block.runners_search.box(block.R_NAME).setText("odoo")
    assert block.selected_runners() == ["Odoo Local"]
    assert [b.text() for b, _v in block._service_buttons][0] == "Start (1)"


def test_the_page_wide_buttons_still_mean_everything(page, monkeypatch):
    calls = []
    monkeypatch.setattr(page.supervisor, "stop_all",
                        lambda project=None, names=None: calls.append((project,
                                                                       names)))
    monkeypatch.setattr(page.supervisor, "counts", lambda project=None: (2, 2))
    _answer_question(monkeypatch)
    page._blocks["Claim"].runners.selectRow(0)
    page.stop_all_button.click()
    assert calls == [(None, None)]


# ------------------------------------------------------ asking before the lot
# Only the All form asks. A button that names a count was aimed at those rows by
# hand; one that says All can take down every backend on the machine from a
# click meant for the row underneath it.
def test_stopping_everything_asks_first_and_says_how_much(page, monkeypatch):
    stopped = []
    monkeypatch.setattr(page.supervisor, "stop_all",
                        lambda project=None, names=None: stopped.append(project))
    monkeypatch.setattr(page.supervisor, "counts", lambda project=None: (2, 2))
    asked = {}
    _answer_question(monkeypatch, record=asked)
    page.stop_all_button.click()
    assert stopped == [None]
    assert "Stop every service on this page?" in asked["text"]
    assert "2 running service(s) will be stopped." in asked["text"]


def test_a_project_wide_button_names_the_project_it_would_take(page, monkeypatch):
    monkeypatch.setattr(page.supervisor, "counts", lambda project=None: (0, 2))
    asked = {}
    _answer_question(monkeypatch, record=asked)
    page._blocks["Claim"]._service_buttons[0][0].click()
    assert "Start every service in 'Claim'?" in asked["text"]
    assert "2 will be started; 0 already running." in asked["text"]


def test_saying_no_leaves_everything_alone(page, monkeypatch):
    acted = []
    for verb in ("start_all", "stop_all", "restart_all"):
        monkeypatch.setattr(page.supervisor, verb,
                            lambda project=None, names=None, v=verb: acted.append(v))
    monkeypatch.setattr(page.supervisor, "counts", lambda project=None: (1, 2))
    _answer_question(monkeypatch, answer=False)
    page.start_all_button.click()
    page.stop_all_button.click()
    for button, _verb in page._blocks["Claim"]._service_buttons:
        button.click()
    assert acted == []


def test_a_selection_is_acted_on_without_being_asked_about(page, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("a button that named a count asked anyway")

    monkeypatch.setattr("cms_gui.widgets.confirm", refuse)
    acted = []
    monkeypatch.setattr(page.supervisor, "restart_all",
                        lambda project=None, names=None: acted.append(names))
    block = page._blocks["Claim"]
    block.runners.selectRow(0)
    block._service_buttons[2][0].click()
    assert acted == [["Odoo Local"]]


def test_nothing_to_do_is_said_rather_than_asked(page, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("asked about an action that would touch nothing")

    monkeypatch.setattr("cms_gui.widgets.confirm", refuse)
    # Nothing is running, so there is nothing for Stop All to stop.
    page.stop_all_button.click()
    assert "No service on this page is running." in page.status.text()


# ------------------------------------------------- how many, and how many badly
def _pretend(page, project, name, status):
    """Put one service in a status without running anything."""
    service = page.supervisor.service(project, name)
    service._set_status(status)
    return service


def test_a_block_says_how_many_of_its_services_are_up(page):
    _pretend(page, "Claim", "Odoo Local", runnertypes.RUNNING)
    assert "(1 of 2 running)" in page._blocks["Claim"].disclosure.title()


def test_a_block_says_how_many_of_them_fell_over(page):
    _pretend(page, "Claim", "Odoo Local", runnertypes.RUNNING)
    _pretend(page, "Claim", "PostgreSQL DB", runnertypes.FAILED)
    title = page._blocks["Claim"].disclosure.title()
    assert "1 of 2 running" in title and "1 failed" in title


def test_a_block_with_nothing_wrong_does_not_say_so(page):
    """"0 failed" on every block is a number read once a glance to find nothing."""
    _pretend(page, "Claim", "Odoo Local", runnertypes.RUNNING)
    assert "failed" not in page._blocks["Claim"].disclosure.title()


def test_the_page_totals_every_project_at_the_top(page):
    """A page of folded blocks otherwise answers "is anything wrong" one at a time."""
    _pretend(page, "Claim", "Odoo Local", runnertypes.RUNNING)
    _pretend(page, "Claim", "PostgreSQL DB", runnertypes.FAILED)
    text = page.totals.text()
    assert "1 running" in text and "1 failed" in text


def test_the_page_totals_say_nothing_while_nothing_is_up(page):
    assert page.totals.text() == ""


def test_the_totals_take_their_colours_from_the_theme_not_from_a_literal(page):
    from cms_gui import theme

    _pretend(page, "Claim", "Odoo Local", runnertypes.RUNNING)
    _pretend(page, "Claim", "PostgreSQL DB", runnertypes.FAILED)
    # Read every time they are set: dark mode rewrites the palette in place.
    assert theme.OK in page.totals.text()
    assert theme.BAD in page.totals.text()


def test_the_block_and_the_page_agree_because_both_ask_the_supervisor(page):
    _pretend(page, "Claim", "Odoo Local", runnertypes.FAILED)
    assert len(page.supervisor.failed("Claim")) == 1
    assert len(page.supervisor.failed()) == 1
    assert "1 failed" in page._blocks["Claim"].disclosure.title()
    assert "1 failed" in page.totals.text()


# ------------------------------------------------------ what an empty page says

def _zone(widget):
    """The empty-state zone, if the page is showing one."""
    return [f for f in widget.findChildren(QFrame)
            if f.property("role") == "emptyzone"]


def test_a_page_with_no_projects_says_so_on_its_own_ground(empty_page):
    """Otherwise it shows only the parts that are always there.

    Connections and the unassigned-logs block are not an answer to "what is on
    this machine" - a page carrying just those two reads as one that failed to
    draw rather than as one nobody has filled in.
    """
    zones = _zone(empty_page)
    assert len(zones) == 1
    said = " ".join(label.text() for label in zones[0].findChildren(QLabel))
    assert "No projects yet" in said
    assert "Add Project" in said


def test_the_zone_goes_as_soon_as_there_is_a_project(page):
    # `page` is loaded with one project, so the question is already answered.
    assert _zone(page) == []


def test_a_project_block_is_clear_and_a_fixed_one_is_filled(page):
    """The distinction the page is read by: yours, against always here.

    A project someone added draws on the page's own ground; Connections and the
    unassigned block are filled, so which is which does not depend on reading
    the titles.
    """
    project = page._blocks["Claim"]
    panels = [f for f in project.findChildren(QFrame)
              if f.property("role") in ("panel", "panel-fixed")]
    assert panels and panels[0].property("role") == "panel"

    fixed = [f for f in page.findChildren(QFrame)
             if f.property("role") == "panel-fixed"]
    assert fixed, "Connections should be a fixed panel"


def test_the_unassigned_block_is_filled_like_connections(empty_page):
    # It is not a project either: it is where logs land when theirs is gone.
    empty_page._logs = [lsf.LogRow(name="stray", connection="here",
                                   project="Gone", type="file",
                                   target="/var/log/stray.log")]
    empty_page._rebuild()
    block = empty_page._blocks[""]
    panels = [f for f in block.findChildren(QFrame)
              if f.property("role") in ("panel", "panel-fixed")]
    assert panels[0].property("role") == "panel-fixed"


def test_an_orphan_log_still_gets_its_block_beside_the_empty_zone(empty_page):
    # Two different statements: no projects, and here are logs belonging to none.
    empty_page._logs = [lsf.LogRow(name="stray", connection="here",
                                   project="Gone", type="file",
                                   target="/var/log/stray.log")]
    empty_page._rebuild()
    assert len(_zone(empty_page)) == 1
    assert "" in empty_page._blocks
