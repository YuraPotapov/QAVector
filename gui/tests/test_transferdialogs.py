"""File -> Export and File -> Import: what is ticked is what goes in the file,
and an import with something already here asks once what to do with it."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt

from cms_gui import core, launchprojectsfile as lpf, transfer, usersfile
from cms_gui.pages.transferdialogs import ExportDialog, ImportDialog

LOCAL, STAGE = "localhost:8069", "https://stage.example.invalid/"


def _setup():
    inventory = core.Inventory({"envs": [
        {"alias": "localhost", "value": LOCAL, "origin": "http://localhost:8069"},
        {"alias": "stage", "value": STAGE, "origin": "https://stage.example.invalid"}]})
    projects = [lpf.ProjectRow(lpf.WORKSPACE, lpf.WORKSPACE_NAME, [
        lpf.EnvironmentRow("env:" + LOCAL, "Local", LOCAL)])]
    rows = [usersfile.UserRow(LOCAL, "Agent", "agent", "pw"),
            usersfile.UserRow(STAGE, "Admin", "admin", "pw")]
    cycles = [{"id": "ready", "name": "Ready", "project": "Claim", "path": "/x"},
              {"id": "nightly", "name": "Nightly", "project": "Claim", "path": "/z"},
              {"id": "loose", "name": "Loose", "project": "", "path": "/y"}]
    return projects, lpf.catalog(projects, inventory), rows, cycles


def _items(dialog):
    return {item.data(0, Qt.UserRole + 2): item for item in dialog._walk()
            if item.data(0, Qt.UserRole + 2) is not None}


def test_a_project_ticked_ticks_its_environments_and_brings_their_users(qapp, dispose):
    projects, catalog, rows, cycles = _setup()
    dialog = ExportDialog(None, projects, catalog, rows, cycles,
                          {"projects": [lpf.WORKSPACE]})
    try:
        items = _items(dialog)
        assert items["env:" + STAGE].checkState(0) == Qt.Checked
        assert dialog.selection()["projects"] == [lpf.WORKSPACE]
        assert dialog.summary.text() == "In the file: 1 project, 2 environments, 2 users"
        items["env:" + STAGE].setCheckState(0, Qt.Unchecked)
        chosen = dialog.selection()
        assert (chosen["projects"], chosen["environments"]) == ([], ["env:" + LOCAL])
        assert dialog.summary.text() == "In the file: 1 project, 1 environment, 1 user"
    finally:
        dispose(dialog)


def test_nothing_ticked_exports_nothing(qapp, dispose):
    dialog = ExportDialog(None, *_setup())
    try:
        assert dialog.summary.text() == "In the file: nothing"
        assert not dialog.export_button.isEnabled()
    finally:
        dispose(dialog)


def test_users_and_cycles_ticked_from_a_page(qapp, dispose):
    projects, catalog, rows, cycles = _setup()
    dialog = ExportDialog(None, projects, catalog, rows, cycles,
                          {"users": [("admin", "pw", "Admin")], "cycles": ["ready"]})
    try:
        chosen = dialog.selection()
        assert chosen["users"] == [("admin", "pw", "Admin")]
        assert chosen["cycles"] == ["ready"] and chosen["cycle_projects"] == []
        assert dialog.suggested_name().startswith("qavector-export-")
    finally:
        dispose(dialog)


def test_one_thing_names_the_file(qapp, dispose):
    projects, catalog, rows, cycles = _setup()
    dialog = ExportDialog(None, projects, catalog, rows, cycles,
                          {"environments": ["env:" + LOCAL]})
    try:
        assert dialog.suggested_name().startswith("qavector-Local-")
        assert dialog.suggested_name().endswith(transfer.SUFFIX)
    finally:
        dispose(dialog)


def test_an_import_asks_once_only_when_something_is_here(qapp, dispose):
    items = [transfer.Item("Environment", "Workspace / Local", True),
             transfer.Item("User", "viewer", False)]
    document = transfer.bundle({"environments": [{}], "users": [{}]})
    dialog = ImportDialog(None, "/tmp/file.qavector.json", document, items)
    try:
        assert dialog.merge.isVisibleTo(dialog) and dialog.copy.isVisibleTo(dialog)
        assert dialog.mode() == transfer.MERGE
        dialog.copy.setChecked(True)
        assert dialog.mode() == transfer.COPY
        assert [dialog.table.item(r, 2).text() for r in range(2)] == ["already here",
                                                                     "new"]
    finally:
        dispose(dialog)
    fresh = ImportDialog(None, "/tmp/file.qavector.json", document,
                         [transfer.Item("User", "viewer", False)])
    try:
        assert not fresh.merge.isVisibleTo(fresh)
    finally:
        dispose(fresh)
