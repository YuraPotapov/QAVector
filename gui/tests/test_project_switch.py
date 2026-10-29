"""Picking a project in the top bar: every page reads that project's folder."""

import os

import pytest

from cms_gui import main_window as mw, projectsfile as pf
from cms_gui.settings import Settings


def _wait(qapp, window, seconds=60):
    """Until the inventory is read and no read follows it: quiet for a while."""
    import time
    end = time.monotonic() + seconds
    quiet_since = None
    while time.monotonic() < end:
        qapp.processEvents()
        thread = window.loader_thread
        busy = (thread is not None and thread.isRunning()) or window._inventory_stale
        if busy:
            quiet_since = None
        elif quiet_since is None:
            quiet_since = time.monotonic()
        elif time.monotonic() - quiet_since > 0.3:
            return
        time.sleep(0.02)


@pytest.fixture
def window(qapp, dispose, tmp_path, monkeypatch):
    # The list and the folders of this test's own, nowhere near anybody's data.
    monkeypatch.setattr(pf, "default_path", lambda: str(tmp_path / pf.FILE_NAME))
    monkeypatch.setattr(pf, "projects_root", lambda: str(tmp_path / "projects"))
    Settings().current_project = pf.WORKSPACE
    win = mw.MainWindow()
    win.show()
    _wait(qapp, win)
    yield win
    win.switch_project(pf.WORKSPACE)
    _wait(qapp, win)
    Settings().current_project = pf.WORKSPACE
    dispose(win)


def test_the_bar_lists_workspace_and_the_way_to_add_one(window):
    combo = window.project_combo
    items = [combo.itemText(i) for i in range(combo.count()) if combo.itemText(i)]
    assert items[0] == "Workspace" and items[-1] == "New…"
    assert combo.currentData() == pf.WORKSPACE


def test_a_new_project_is_an_empty_application(window, qapp, monkeypatch):
    from cms_gui import widgets
    monkeypatch.setattr(widgets, "ask_text", lambda *a, **k: "CLAIM")
    window.add_project()
    _wait(qapp, window)
    project = pf.find(pf.load(), window.settings.project_id)
    assert project.name == "CLAIM" and window.project_combo.currentText() == "CLAIM"
    # Every file is the project's own, and there is nothing in them.
    assert window.core.config_path == pf.paths(project.folder).users
    assert window.core.flows_dir == pf.paths(project.folder).flows
    assert window.launch_registry.path == pf.paths(project.folder).launch_projects
    assert window.history.directory == project.folder
    assert window.environments.table.rowCount() == 0
    assert window.credentials.table.rowCount() == 0
    assert window.inventory.users == [] and window.inventory.scenarios == []
    assert window.launch.controller.presets.all() == []
    # And back: Workspace as it was.
    window.switch_project(pf.WORKSPACE)
    _wait(qapp, window)
    assert window.settings.project_folder == ""
    assert window.core.config_path != pf.paths(project.folder).users


def test_page_state_is_kept_per_project(window, qapp):
    project = pf.add("Helpdesk")
    window.settings.save_folds("test-page", {"workspace": True})
    window.switch_project(project.id)
    _wait(qapp, window)
    assert window.settings.folds("test-page") == {}
    window.settings.save_folds("test-page", {"helpdesk": True})
    window.switch_project(pf.WORKSPACE)
    _wait(qapp, window)
    assert window.settings.folds("test-page") == {"workspace": True}
    window.settings.save_folds("test-page", {})


def test_a_project_cannot_be_left_during_a_run(window, monkeypatch):
    from cms_gui import widgets
    project = pf.add("Busy")
    said = []
    with monkeypatch.context() as patch:
        patch.setattr(widgets, "note", lambda *a, **k: said.append(a[2]))
        patch.setattr(window.process, "is_running", lambda: True)
        assert window.switch_project(project.id) is False
    assert window.settings.project_id == pf.WORKSPACE and said


def test_an_unsaved_edit_is_asked_about_first(window, monkeypatch):
    from cms_gui import widgets
    project = pf.add("Edited")
    with monkeypatch.context() as patch:
        patch.setattr(window.credentials, "is_dirty", lambda: True)
        patch.setattr(widgets, "confirm", lambda *a, **k: False)
        assert window.switch_project(project.id) is False
    assert window.settings.project_id == pf.WORKSPACE


def test_rename_and_delete_from_the_environments_page(window, qapp, monkeypatch):
    from cms_gui import widgets
    project = pf.add("Old")
    window.switch_project(project.id)
    _wait(qapp, window)
    monkeypatch.setattr(widgets, "ask_text", lambda *a, **k: "New")
    window.environments.rename_project_button.click()
    assert window.project_combo.currentText() == "New"
    monkeypatch.setattr(widgets, "confirm", lambda *a, **k: True)
    window.environments.delete_project_button.click()
    _wait(qapp, window)
    assert window.settings.project_id == pf.WORKSPACE
    assert pf.find(pf.load(), project.id) is None
    assert os.path.isdir(project.folder), "its data stays on disk"


def test_search_finds_another_project_and_opening_picks_it(window, qapp):
    import json as _json
    project = pf.add("CLAIM")
    paths = pf.paths(project.folder)
    with open(paths.users, "w", encoding="utf-8") as handle:
        _json.dump([{"env": "claim-local", "class": "Agent", "login": "claim-agent",
                     "password": "pw"}], handle)
    with open(os.path.join(paths.cycles, "release.yaml"), "w", encoding="utf-8") as handle:
        handle.write("id: release\nname: Release gate\nsteps: []\n")
    hits = [hit for hit in window.search_hits() if hit.project == project.id]
    by_section = {hit.section: hit for hit in hits}
    assert by_section["credentials"].title == "claim-agent"
    assert by_section["credentials"].detail.endswith("CLAIM")
    assert by_section["cycles"].title == "Release gate"
    assert by_section["environments"].target == "env:claim-local"
    window.open_hit(by_section["credentials"])
    _wait(qapp, window)
    qapp.processEvents()
    assert window.settings.project_id == project.id
    assert window._page_key == "credentials"
    rows = window.credentials.table.selectionModel().selectedRows()
    assert [r.row() for r in rows] == [0]
    # From inside CLAIM, Workspace is searched the same way.
    assert all(hit.project != project.id for hit in window.search_hits())


def test_nothing_of_the_project_left_shows_while_the_next_is_read(window, qapp):
    project = pf.add("Next")
    window.switch_project(project.id)
    # Straight after the switch, before the core has answered:
    assert window.switching.active()
    assert window.inventory.users == [] and window.inventory.envs == []
    assert window.launch.users_list.rows() == []
    assert window.environments.table.rowCount() == 0
    _wait(qapp, window)
    assert not window.switching.active()


def test_an_empty_project_says_it_has_no_environments(window, qapp):
    project = pf.add("Empty")
    window.switch_project(project.id)
    _wait(qapp, window)
    launch = window.launch
    assert launch.env_combo.count() == 0
    assert launch.env_combo.placeholderText() == "No environments yet"
    assert "No environments in this project yet" in launch.env_note.text()


def test_switching_writes_nothing_of_the_project_left_into_the_next(window, qapp):
    """The context Launch Sessions was at must not land in the new project's
    files - it came back there as "env:... (missing)"."""
    import json as _json
    window.settings.save_launch_context({"environment": "env:localhost:8069"})
    window.launch.controller.environment_id = "env:localhost:8069"
    project = pf.add("Clean")
    window.switch_project(project.id)
    _wait(qapp, window)
    launch = window.launch
    assert launch.env_combo.count() == 0
    assert "env:" not in window.settings.launch_context().get("environment", "")
    drafts = os.path.join(project.folder, "launch-drafts.json")
    if os.path.exists(drafts):
        keys = (_json.load(open(drafts)).get("drafts") or {}).keys()
        assert not [key for key in keys if "env:" in key], keys


def test_a_context_left_over_from_before_is_not_offered(window, qapp):
    project = pf.add("Leftover")
    window.switch_project(project.id)
    _wait(qapp, window)
    window.settings.save_launch_context({"environment": "missing:env:old"})
    window.switch_project(pf.WORKSPACE)
    _wait(qapp, window)
    window.switch_project(project.id)
    _wait(qapp, window)
    assert window.launch.env_combo.count() == 0
