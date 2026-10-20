"""Restrictions in the GUI: the folder button, the quick toggles, the window.

The core resolves and enforces; these check what a person sees and what each
click writes. Nothing here starts a sandbox.
"""

import copy

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QDialog

from cms_gui import restrictions as R
from cms_gui import runnertypes, servicesfile as sf
from cms_gui.pages import cycles as cycles_mod
from cms_gui.pages.cycledialogs import CycleDialog, NodeDialog
from cms_gui.pages.cycles import CyclesPage
from cms_gui.pages.services import ProjectDialog, RunnerDialog, ServicesPage
from cms_gui.settings import Settings
from test_cycles_page import FakeCore, FakeInventory, SHELL_PLUGIN, graph, payload, row


# -- the stored mapping ----------------------------------------------------------------
def test_switching_off_and_back_on_restores_exactly_what_was_configured():
    configured = {"network": "on", "files": "read_only", "limits": {"memory_mb": 2048}}
    off = R.toggled(configured, "files")
    assert off["switched_off"] == ["files"] and off["files"] == "read_only"
    assert R.resolve([("project", off)])["files"] == "none"
    assert R.toggled(off, "files") == configured
    assert R.switch_state([], off, "project", "files")[:2] == ("Off", "off")
    assert R.switch_state([], configured, "project", "files")[:2] == ("RO", "on")


def test_off_by_the_project_default_turns_on_for_one_service_with_a_click():
    outer = [("project", {"switched_off": ["network"], "files": "read_only"})]
    shown, state, why = R.switch_state(outer, {}, "service", "network")
    assert (shown, state) == ("Off", "default")
    assert "project's default" in why
    assert R.click(outer, {}, "service", "network") == {"network": "on"}
    assert R.resolve(outer + [("service", {"network": "on"})])["network"] == "on"


def test_every_off_click_turns_access_back_on_where_it_can():
    assert R.click([], {"network": "off"}, "project", "network") == {}
    assert R.click([], {"switched_off": ["files"], "files": "read_only"}, "project", "files") == {"files": "read_only"}
    outer = [("project", {"files": "read_write", "switched_off": ["files"]})]
    assert R.click(outer, {}, "service", "files") == {"files": "read_write"}
    assert R.click([], {"files": "none"}, "project", "files") is None


def test_pending_names_only_what_a_change_touches():
    assert R.pending({"network": "off"}, {"network": "off", "limits": {"processes": 64}}) == ("limits",)
    assert R.pending({}, {"switched_off": ["network"]}) == ("network",)
    assert R.pending({"files": "read_only"}, {"files": "read_only"}) == ()


def test_on_and_off_written_unquoted_in_yaml_still_mean_on_and_off():
    assert R.resolve([("cycle", {"network": False})])["network"] == "off"
    assert R.network_value(True) == "on"
    assert R.problems({"network": False}, "cycle") == []


def test_summaries_always_say_which_way_the_network_is():
    effective = R.resolve([("project", {"network": "off", "files": "read_write",
                                        "limits": {"memory_mb": 4096, "cpu_cores": 2}})])
    assert R.summary(effective) == "Network off · Project read and write · 4 GB · 2 CPU"
    assert R.short_summary(effective) == "Net off · RW · 4 GB · 2 CPU"
    assert R.short_summary(R.resolve([("project", {"files": "read_only"})])) == "Net on · RO"
    assert R.summary(R.resolve([])) == "Network on · No restrictions"
    assert R.short_summary(R.resolve([])) == "Net on · No restrictions"


# -- the quick toggles -------------------------------------------------------------------
def test_quick_toggles_show_state_in_words_and_marks(qapp, dispose):
    toggles = R.QuickToggles()
    try:
        toggles.set_model([("project", {"files": "read_only", "limits": {"memory_mb": 4096}})],
                          {"switched_off": ["network"]}, "service", pending=("network",))
        assert toggles.network.text() == "Off" and toggles.network.property("state") == "off"
        assert toggles.network.property("pending") == "true"
        assert toggles.files.text() == "RO" and toggles.files.property("state") == "on"
        assert toggles.limits.text() == "4 GB"
        assert "Not saved yet" in toggles.network.toolTip()
    finally:
        dispose(toggles)


def test_a_click_switches_or_explains(qapp, dispose):
    toggles = R.QuickToggles()
    seen = []
    toggles.toggle.connect(lambda kind: seen.append(("toggle", kind)))
    toggles.open_window.connect(lambda: seen.append(("window",)))
    try:
        toggles.set_model([("project", {"network": "off"})], {}, "service")
        toggles.network.click()
        toggles.set_model([], {"files": "none"}, "project")
        toggles.files.click()
        assert seen == [("toggle", "network"), ("window",)]
    finally:
        dispose(toggles)


def test_every_row_s_toggles_are_the_same_width(qapp, dispose):
    one, two = R.QuickToggles(), R.QuickToggles()
    try:
        one.set_model([], {}, "service")
        two.set_model([], {"network": "off", "files": "read_only", "limits": {"memory_mb": 16384, "cpu_cores": 2}}, "service")
        for name in ("network", "files", "limits"):
            assert getattr(one, name).width() == getattr(two, name).width()
    finally:
        dispose(one)
        dispose(two)


# -- the window -------------------------------------------------------------------------
def test_a_service_window_starts_from_the_project_defaults_and_can_set_its_own(qapp, dispose):
    window = R.RestrictionsWindow("Tests", "/work/shop", "service", {"switched_off": ["network"]},
                                  outer=[("project", {"files": "read_only", "network": "off"})], outer_name="project")
    try:
        assert window.files.button("Read and write").isEnabled()
        assert window.files.current() == "Same as project" and window.network.current() == "Same as project"
        assert window.value() == {"switched_off": ["network"]}
        assert not window.banner.isHidden()
        window.switch_back.click()
        assert window.value() == {}
        window.network.set_current("On")
        window.files.set_current("Read and write")
        assert window.value() == {"network": "on", "files": "read_write"}
    finally:
        dispose(window)


def test_the_window_refuses_a_limit_that_is_not_a_number(qapp, dispose):
    window = R.RestrictionsWindow("Shop", "/work/shop", "project", {})
    try:
        edit, _unit = window.limits.fields["processes"]
        edit.setText("3")
        window._changed()
        from PySide6.QtWidgets import QDialogButtonBox
        assert "at least 8" in window.problem.text()
        assert not window.buttons.button(QDialogButtonBox.Ok).isEnabled()
    finally:
        dispose(window)


def test_a_cycle_window_lists_its_steps(qapp, dispose):
    window = R.RestrictionsWindow("Demo", "/work/shop", "cycle", {"network": "off"},
                                  steps=[("Build", "Same as the cycle"), ("Report", "System step - runs without restrictions")])
    try:
        assert "Steps" in [label.text() for label in window.findChildren(type(window.summary))]
    finally:
        dispose(window)


# -- services ---------------------------------------------------------------------------
def page_with(project_dir="/work/shop", project_restrictions=None, runner_restrictions=None):
    runners = [sf.RunnerRow("App", "python", settings={"script": "app.py"},
                            extra={"restrictions": runner_restrictions} if runner_restrictions else {}),
               sf.RunnerRow("DB", "docker", settings={"container": "db"})]
    project = sf.ProjectRow("Shop", project_dir, runners=runners,
                            extra={"restrictions": project_restrictions} if project_restrictions else {})
    page = ServicesPage()
    page._projects = [project]
    page._rebuild()
    page._mark_clean()
    return page


def strip(page, name):
    block = page._blocks["Shop"]
    index = [one.name for one in block._runner_rows].index(name)
    return block.runners.cellWidget(index, block.R_ACTIONS)


def test_a_project_without_a_folder_keeps_its_rows_as_they_were(qapp, dispose):
    page = page_with(project_dir="")
    try:
        assert strip(page, "App").restrictions is None
        assert page._blocks["Shop"].project_toggles is None
    finally:
        dispose(page)


def test_a_container_row_keeps_the_column_but_shows_no_toggles(qapp, dispose):
    page = page_with()
    try:
        assert strip(page, "App").restrictions.isVisibleTo(page)
        hidden = strip(page, "DB").restrictions
        assert hidden is not None and not hidden.isVisibleTo(page)
        assert hidden.sizePolicy().retainSizeWhenHidden()
    finally:
        dispose(page)


def test_a_quick_toggle_changes_the_draft_and_a_second_click_takes_it_back(qapp, dispose):
    page = page_with(project_restrictions={"files": "read_write"})
    try:
        page.toggle_restriction("Shop", "App", "network")
        row = page._project("Shop").runner("App")
        assert row.extra["restrictions"] == {"switched_off": ["network"]}
        assert page.is_dirty()
        assert page.pending_restrictions("Shop", "App") == ("network",)
        assert strip(page, "App").restrictions.network.text() == "Off"
        page.toggle_restriction("Shop", "App", "network")
        assert "restrictions" not in page._project("Shop").runner("App").extra
        assert not page.is_dirty()
    finally:
        dispose(page)


def test_the_project_toggle_is_the_default_and_one_service_can_turn_its_own_back_on(qapp, dispose):
    page = page_with(project_restrictions={"files": "read_write"})
    try:
        page.toggle_restriction("Shop", None, "network")
        assert page._project("Shop").extra["restrictions"]["switched_off"] == ["network"]
        toggles = strip(page, "App").restrictions
        assert toggles.network.text() == "Off" and toggles.network.property("state") == "locked"
        page.toggle_restriction("Shop", "App", "network")
        assert page._project("Shop").runner("App").extra["restrictions"] == {"network": "on"}
        assert strip(page, "App").restrictions.network.text() == "On"
    finally:
        dispose(page)


def test_the_project_dialog_calls_it_the_project_folder_and_keeps_restrictions(qapp, dispose):
    project = sf.ProjectRow("Shop", "/work/shop", extra={"restrictions": {"network": "off"}})
    dialog = ProjectDialog(project)
    try:
        labels = [label.text() for label in dialog.findChildren(type(dialog.problem))]
        assert "Project folder" in labels
        assert dialog.restrict_button.isEnabled()
        assert dialog.value().extra["restrictions"] == {"network": "off"}
        dialog.dir.setText("")
        assert not dialog.restrict_button.isEnabled()
    finally:
        dispose(dialog)


def test_the_service_dialog_puts_the_button_beside_its_working_directory(qapp, dispose):
    row = sf.RunnerRow("App", "python", settings={"script": "app.py"}, extra={"restrictions": {"files": "read_only"}})
    dialog = RunnerDialog(runnertypes.get("python"), row, project_dir="/work/shop",
                          project_restrictions={"network": "off"})
    docker = RunnerDialog(runnertypes.get("docker"), sf.RunnerRow("DB", "docker"), project_dir="/work/shop")
    try:
        assert dialog.restrict_button is not None and dialog.restrict_button.isEnabled()
        assert "RO" in dialog.restrict_button.text()
        assert dialog.value().extra["restrictions"] == {"files": "read_only"}
        assert docker.restrict_button is None
    finally:
        dispose(dialog)
        dispose(docker)


class _Core:
    prefix = ["qavector-core"]

    def is_configured(self):
        return True

    def spawn_dir(self):
        return ""


def test_a_restricted_service_starts_through_the_core(qapp, tmp_path):
    from cms_gui import services
    supervisor = services.ServiceSupervisor()
    row = sf.RunnerRow("App", "shell", settings={"command": "serve --port 8000"})
    supervisor.sync([sf.ProjectRow("Shop", str(tmp_path), runners=[row],
                                   extra={"restrictions": {"network": "off"}})])
    service = supervisor.service("Shop", "App")
    service.runtime_core = _Core()
    started = []
    service._start_attached = lambda argv, cwd, marker=None: started.append((argv, marker))
    try:
        assert service.start()
        argv, marker = started[0]
        assert argv[0] == "qavector-core" and argv[1].startswith("--restricted-exec=")
        assert argv[2] == "--" and marker == argv[3]
        assert service.restricted_with == [("project", {"network": "off"}), ("service", {})]
    finally:
        supervisor.dispose()


def test_restrictions_with_no_core_to_enforce_them_refuse_to_start(qapp, tmp_path):
    from cms_gui import services
    supervisor = services.ServiceSupervisor()
    row = sf.RunnerRow("App", "shell", settings={"command": "serve"})
    supervisor.sync([sf.ProjectRow("Shop", str(tmp_path), runners=[row],
                                   extra={"restrictions": {"network": "off"}})])
    service = supervisor.service("Shop", "App")
    try:
        assert not service.start()
        assert "not started" in service.detail
    finally:
        supervisor.dispose()


def test_a_service_in_a_folder_with_nothing_set_starts_exactly_as_before(qapp, tmp_path):
    from cms_gui import services
    supervisor = services.ServiceSupervisor()
    supervisor.sync([sf.ProjectRow("Shop", str(tmp_path), runners=[sf.RunnerRow("App", "shell", settings={"command": "serve"})])])
    service = supervisor.service("Shop", "App")
    started = []
    service._start_attached = lambda argv, cwd, marker=None: started.append(argv)
    try:
        assert service.start()
        assert "--restricted-exec" not in " ".join(started[0])
    finally:
        supervisor.dispose()


# -- cycles -----------------------------------------------------------------------------
def cycle_page(document_extra=None):
    made = graph(("build", []), ("notify", ["build"]))
    made["nodes"][1]["plugin"] = "report.json"
    document = {"id": "demo", "name": "Demo cycle",
                "variables": {"project_dir": {"kind": "path", "value": "/work/shop"}},
                "steps": [{"id": "build", "plugin": "command.shell", "with": {"command": "make"}},
                          {"id": "notify", "plugin": "report.json", "needs": ["build"]}]}
    document.update(document_extra or {})
    page = CyclesPage(Settings())
    page.set_core(FakeCore({"demo": payload(cycle=made, document=document)}))
    page.set_inventory(FakeInventory([row()], [SHELL_PLUGIN]))
    page.open("demo")
    return page


def test_the_restrictions_button_needs_a_project_folder(qapp, dispose):
    plain = cycle_page()
    held = cycle_page({"project_folder": "${vars.project_dir}", "restrictions": {"files": "read_only"}})
    try:
        assert not plain.restrictions_button.isEnabled()
        assert "Properties" in plain.restrictions_button.toolTip()
        assert held.restrictions_button.isEnabled() and held.restrictions_button.text() == "Net on · RO"
        assert held.network_toggle.isVisibleTo(held) and held.network_toggle.text() == "On"
        assert not plain.network_toggle.isVisibleTo(plain)
        assert held._folder_of(held._document_now()) == ("${vars.project_dir}", "/work/shop")
    finally:
        dispose(plain)
        dispose(held)


def test_properties_and_restrictions_open_different_windows(qapp, dispose, monkeypatch):
    opened = []

    class Recorder(QDialog):
        def __init__(self, *args, **kwargs):
            super().__init__()
            opened.append(type(self).kind)
            self.saved = None

        def exec(self):
            return QDialog.Rejected

    monkeypatch.setattr(cycles_mod, "CycleDialog", type("Properties", (Recorder,), {"kind": "properties"}))
    monkeypatch.setattr(R, "RestrictionsWindow", type("Restrictions", (Recorder,), {"kind": "restrictions"}))
    page = cycle_page({"project_folder": "/work/shop"})
    try:
        page.properties_button.click()
        page.restrictions_button.click()
        assert opened == ["properties", "restrictions"]
    finally:
        dispose(page)


def test_a_node_indicator_switches_into_the_draft_and_undo_takes_it_back(qapp, dispose):
    page = cycle_page({"project_folder": "/work/shop", "restrictions": {"files": "read_write"}})
    try:
        page._restriction_clicked("build", "network", QPoint(0, 0))
        step = page._step_of("build")
        assert step["restrictions"] == {"switched_off": ["network"]}
        state = page.canvas.node("build").restriction
        assert state["network"] == ("Off", "off") and state["pending"] == {"network"}
        page._undo()
        assert "restrictions" not in page._step_of("build")
    finally:
        dispose(page)


def test_system_steps_show_a_tag_and_take_no_clicks(qapp, dispose):
    page = cycle_page({"project_folder": "/work/shop"})
    try:
        item = page.canvas.node("notify")
        assert item.restriction == {"system": True}
        assert item.restriction_at(item.restriction_rect("network").center()) == ""
        build = page.canvas.node("build")
        assert build.restriction_at(build.restriction_rect("files").center()) == "files"
    finally:
        dispose(page)


def test_the_toolbar_network_toggle_goes_through_the_draft(qapp, dispose):
    page = cycle_page({"project_folder": "/work/shop"})
    try:
        page.network_toggle.click()
        assert page._document_now()["restrictions"] == {"switched_off": ["network"]}
        assert page.network_toggle.text() == "Off" and page.network_toggle.property("pending") == "true"
        assert page.canvas.node("build").restriction["network"][0] == "Off"
        page._undo()
        assert "restrictions" not in page._document_now()
        assert page.network_toggle.text() == "On"
    finally:
        dispose(page)


def test_a_cycle_without_a_folder_draws_no_indicators(qapp, dispose):
    page = cycle_page()
    try:
        assert page.canvas.node("build").restriction is None
    finally:
        dispose(page)


def test_the_step_window_shows_restrictions_only_when_the_cycle_has_a_folder(qapp, dispose):
    step = {"id": "build", "plugin": "command.shell", "with": {"command": "make"}}
    plain = NodeDialog(step, SHELL_PLUGIN)
    held = NodeDialog(dict(step, restrictions={"network": "off"}), SHELL_PLUGIN,
                      restrictions={"folder": "/work/shop", "cycle": {"files": "read_only"}})
    report = NodeDialog({"id": "notify", "plugin": "report.json"}, {},
                        restrictions={"folder": "/work/shop", "cycle": {}})
    try:
        assert plain.system_box is None
        assert held.system_box.isEnabled() and not held.system_box.isChecked()
        assert "Net off" in held.restrict_button.text()
        assert report.system_box.isChecked() and not report.system_box.isEnabled()
        held.system_box.setChecked(True)
        held._save()
        assert held.saved.get("system") is True and "restrictions" not in held.saved
    finally:
        for one in (plain, held, report):
            dispose(one)


def test_cycle_properties_save_the_project_folder_and_its_restrictions(qapp, dispose):
    document = {"id": "demo", "name": "Demo", "steps": [{"id": "a", "plugin": "command.shell"}],
                "project_folder": "/work/shop", "restrictions": {"network": "off"}}
    dialog = CycleDialog(document)
    try:
        assert dialog.restrict_button.isEnabled()
        dialog._save()
        assert dialog.saved["project_folder"] == "/work/shop"
        assert dialog.saved["restrictions"] == {"network": "off"}
        dialog.project_folder.setText("")
        dialog._save()
        assert dialog.saved["project_folder"] == "" and dialog.saved["restrictions"] == {}
    finally:
        dispose(dialog)
