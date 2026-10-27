"""Restrictions in the GUI: the folder button, the quick toggles, the window.

The core resolves and enforces; these check what a person sees and what each
click writes. Nothing here starts a sandbox.
"""

import copy

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QDialog, QFrame

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


def test_each_toggle_is_as_wide_as_what_it_says(qapp, dispose):
    """No box wider than its word on hover, and nothing kept after the last."""
    toggles = R.QuickToggles()
    try:
        toggles.set_model([], {"network": "off", "limits": {"memory_mb": 16384, "cpu_cores": 2}}, "service")
        toggles.show()
        qapp.processEvents()
        for button in (toggles.network, toggles.files, toggles.limits):
            assert button.width() == button.sizeHint().width()
        assert toggles.width() == toggles.limits.geometry().right() + 1
    finally:
        dispose(toggles)


def test_the_three_toggles_are_evenly_spaced(qapp, dispose):
    """One gap between each two: widths kept for the widest word each could
    show put the marks at uneven distances."""
    toggles = R.QuickToggles()
    try:
        toggles.set_model([], {}, "service")
        toggles.show()
        qapp.processEvents()
        network, files, limits = (one.geometry() for one in (toggles.network, toggles.files, toggles.limits))
        assert files.left() - network.right() == limits.left() - files.right() == R.TOGGLE_GAP + 1
    finally:
        dispose(toggles)


# -- the window -------------------------------------------------------------------------
def test_a_service_window_starts_from_the_project_defaults_and_can_set_its_own(qapp, dispose):
    window = R.RestrictionsWindow("Tests", "/work/shop", "service", {"switched_off": ["network"]},
                                  outer=[("project", {"files": "read_only", "network": "off"})], outer_name="project")
    try:
        assert window.files.button("Read and write").isEnabled()
        assert window.files.current() == "Same as project" and window.network.current() == "Off"
        assert window.value() == {"switched_off": ["network"]}
        window.network.set_current("On")
        window.files.set_current("Read and write")
        assert window.value() == {"network": "on", "files": "read_write"}
    finally:
        dispose(window)


def test_switched_off_shows_as_off_and_keeps_what_is_configured_until_changed(qapp, dispose):
    own = {"network": "on", "files": "read_only", "switched_off": ["network", "files"]}
    window = R.RestrictionsWindow("Shop", "/work/shop", "project", own)
    try:
        # No note about how it was switched off: the switches say it.
        assert window.network.current() == "Off" and window.files.current() == "No access"
        assert window.value() == own
        window.files.set_current("Read and write")
        assert window.value() == {"network": "on", "files": "read_write", "switched_off": ["network"]}
        window.network.set_current("On")
        assert window.value() == {"files": "read_write"}
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


def test_a_container_row_shows_no_toggles_and_its_buttons_stay_in_the_column(qapp, dispose):
    """The row's own buttons keep to the cell's right edge, so a row with no
    toggles - or with wider words in them - leaves them where the others are."""
    from PySide6.QtWidgets import QPushButton
    page = page_with()
    try:
        page.resize(1100, 400)
        page.show()
        qapp.processEvents()
        assert strip(page, "App").restrictions.isVisibleTo(page)
        hidden = strip(page, "DB").restrictions
        assert hidden is not None and not hidden.isVisibleTo(page)

        def console_x(name):
            button = next(one for one in strip(page, name).findChildren(QPushButton) if one.text() == "Console")
            return button.mapTo(page, button.rect().topLeft()).x()

        assert console_x("App") == console_x("DB")
        page.toggle_restriction("Shop", "App", "network")       # "Off" is wider than "On"
        qapp.processEvents()
        assert console_x("App") == console_x("DB")
    finally:
        dispose(page)


def test_a_service_s_toggles_stand_between_two_dividers_and_the_project_s_have_none(qapp, dispose):
    page = page_with()
    try:
        row = strip(page, "App")
        layout = row.layout()
        order = [layout.itemAt(index).widget() for index in range(layout.count())]
        at = order.index(row.restrictions)
        assert order[at - 1].property("role") == order[at + 1].property("role") == "vline"
        header = page._blocks["Shop"].project_toggles.parentWidget()
        assert not [one for one in header.findChildren(QFrame) if one.property("role") == "vline"]
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


def test_a_quick_toggle_repaints_in_place_and_keeps_the_page_where_it_was(qapp, dispose):
    """A rebuild made every block and row again - seconds with the window held
    on a real file - and put the page back at the top."""
    page = page_with()
    try:
        page.resize(900, 300)
        page.show()
        qapp.processEvents()
        block, row_strip = page._blocks["Shop"], strip(page, "App")
        bar = page._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())
        where = bar.value()
        page.toggle_restriction("Shop", None, "files")
        page.toggle_restriction("Shop", "App", "network")
        qapp.processEvents()
        assert page._blocks["Shop"] is block and strip(page, "App") is row_strip
        assert bar.value() == where
        # The row the row's own buttons hold is the row that changed.
        assert block._runner_rows[[one.name for one in block._runner_rows].index("App")] \
            is page._project("Shop").runner("App")
        assert row_strip.restrictions.network.text() == "Off"
    finally:
        dispose(page)


def test_a_folded_project_s_toggles_are_right_when_it_opens(qapp, dispose):
    page = page_with()
    try:
        page.show()
        page._blocks["Shop"].disclosure.set_expanded(False)
        page.toggle_restriction("Shop", None, "network")
        page._blocks["Shop"].disclosure.set_expanded(True)
        qapp.processEvents()
        assert strip(page, "App").restrictions.network.text() == "Off"
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
    made = graph(("build", []), ("notify", ["build"]), ("browse", ["build"]), ("tell", ["build"]))
    for node, plugin in zip(made["nodes"][1:], ("report.json", "scenario.run", "teams.message")):
        node["plugin"] = plugin
    document = {"id": "demo", "name": "Demo cycle",
                "variables": {"project_dir": {"kind": "path", "value": "/work/shop"}},
                "steps": [{"id": "build", "plugin": "command.shell", "with": {"command": "make"}},
                          {"id": "notify", "plugin": "report.json", "needs": ["build"]},
                          {"id": "browse", "plugin": "scenario.run", "needs": ["build"]},
                          {"id": "tell", "plugin": "teams.message", "needs": ["build"]}]}
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
        # The lock is the state and the network has its own toggle: no words repeat it.
        assert held.restrictions_button.isEnabled() and held.restrictions_button.text() == "Restrictions"
        assert held.network_toggle.isVisibleTo(held) and held.network_toggle.text() == "On"
        assert held.network_toggle.toolButtonStyle() == Qt.ToolButtonIconOnly
        assert "on" in held.network_toggle.toolTip()
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


def test_only_steps_that_run_unrestricted_say_system_and_steps_with_nothing_to_hold_say_nothing(qapp, dispose):
    page = cycle_page({"project_folder": "/work/shop", "restrictions": {"switched_off": ["network"]}})
    try:
        browser = page.canvas.node("browse")
        assert browser.restriction == {"system": True}
        assert browser.restriction_at(browser.restriction_rect("network").center()) == ""
        assert page.canvas.node("notify").restriction is None          # a report: nothing to restrict
        build, tell = page.canvas.node("build"), page.canvas.node("tell")
        assert build.restriction_at(build.restriction_rect("files").center()) == "files"
        assert tell.restriction["network"][0] == "Off"                   # Teams is held like the rest
        assert "Will not start: it needs the network" in tell.restriction["summary"]
        assert "Will not start" not in build.restriction["summary"]
        rows = dict(page._steps_for_window(page._document_now()))
        assert "notify" not in rows and rows["browse"].startswith("System step")
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


@pytest.mark.parametrize("zoom", [0.45, 1.0, 2.0])
def test_node_tooltips_are_anchored_above_each_icon_and_hide_when_leaving(qapp, dispose, zoom):
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QHelpEvent, QMouseEvent, QTransform
    from PySide6.QtWidgets import QApplication
    from cms_gui.cyclegraph import CycleCanvas

    canvas = CycleCanvas()
    canvas.resize(620, 420)
    canvas.set_graph(graph(("build", [])))
    canvas.set_restrictions({"build": {
        "network": ("Isolated", "on"), "files": ("RW", "on"),
        "limits": "4 GB", "summary": "Network isolated · Project read and write · Memory 4 GB"}})
    canvas.show()
    qapp.processEvents()
    canvas.setTransform(QTransform.fromScale(zoom, zoom))
    node = canvas.node("build")
    canvas.centerOn(node)
    qapp.processEvents()
    for kind, expected in (("network", "Isolated"), ("files", "RW"), ("limits", "4 GB")):
        rect = node.restriction_rect(kind)
        point = canvas.mapFromScene(node.mapToScene(rect.center()))
        QApplication.sendEvent(canvas.viewport(), QHelpEvent(
            QEvent.ToolTip, point, canvas.viewport().mapToGlobal(point)))
        tip = canvas._restriction_tip
        assert tip.isVisible() and tip.text() == expected
        top = canvas.viewport().mapToGlobal(canvas.mapFromScene(node.mapToScene(
            QPointF(rect.center().x(), rect.top()))))
        assert tip.geometry().bottom() < top.y()
        assert abs(tip.geometry().center().x() - top.x()) <= 1
    # Deliver directly: unrelated offscreen windows may cover this canvas.
    QApplication.sendEvent(canvas.viewport(), QMouseEvent(
        QEvent.MouseMove, QPointF(1, 1),
        QPointF(canvas.viewport().mapToGlobal(QPoint(1, 1))),
        Qt.NoButton, Qt.NoButton, Qt.NoModifier))
    assert not tip.isVisible()
    dispose(canvas)


def test_the_step_window_shows_restrictions_only_when_the_cycle_has_a_folder(qapp, dispose):
    step = {"id": "build", "plugin": "command.shell", "with": {"command": "make"}}
    plain = NodeDialog(step, SHELL_PLUGIN)
    held = NodeDialog(dict(step, restrictions={"network": "off"}), SHELL_PLUGIN,
                      restrictions={"folder": "/work/shop", "cycle": {"files": "read_only"}})
    report = NodeDialog({"id": "notify", "plugin": "report.json"}, {},
                        restrictions={"folder": "/work/shop", "cycle": {}})
    browser = NodeDialog({"id": "browse", "plugin": "scenario.run"}, {},
                         restrictions={"folder": "/work/shop", "cycle": {}})
    try:
        assert plain.system_box is None
        assert held.system_box.isEnabled() and not held.system_box.isChecked()
        assert "Net off" in held.restrict_button.text()
        assert report.system_box is None and report.restrict_button is None
        assert browser.system_box.isChecked() and not browser.system_box.isEnabled()
        held.system_box.setChecked(True)
        held._save()
        assert held.saved.get("system") is True and "restrictions" not in held.saved
    finally:
        for one in (plain, held, report, browser):
            dispose(one)


@pytest.mark.parametrize("suffix", ["", "/checkout"])
def test_properties_use_the_path_value_for_folder_preview_and_restrictions(
        qapp, dispose, monkeypatch, tmp_path, suffix):
    declaration = {"kind": "path", "value": str(tmp_path)}
    written = "${vars.project_dir}" + suffix
    document = {"id": "demo", "name": "Demo", "project_folder": written,
                "variables": {"project_dir": declaration},
                "steps": [{"id": "a", "plugin": "command.shell"}]}
    captured = []

    class Restrictions(QDialog):
        saved = None

        def __init__(self, title, folder, level, restrictions, **kwargs):
            super().__init__(kwargs.get("parent"))
            captured.append((folder, kwargs["probe"]))

        def exec(self):
            return QDialog.Rejected

    monkeypatch.setattr(R, "RestrictionsWindow", Restrictions)
    dialog = CycleDialog(document)
    try:
        expected = str(tmp_path) + suffix
        assert dialog._folder() == (written, expected)
        dialog._edit_restrictions()
        assert captured == [(expected, {"folder": expected, "cwd": expected})]
        dialog._save()
        assert dialog.saved["project_folder"] == written
        assert dialog.saved["variables"]["project_dir"] == declaration
    finally:
        dispose(dialog)


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


# -- an isolated network: its own ports, chosen ones published ---------------------------
def test_an_isolated_network_is_a_third_way_with_what_it_lets_through():
    effective = R.resolve([("project", {"network": "isolated", "outbound": True}),
                           ("service", {"publish": [{"internal_port": 8069}]})])
    assert effective["restricted"] and effective["network"] == "isolated" and effective["outbound"]
    assert effective["publish"] == [{"internal_port": 8069, "host_port": "auto", "host_ip": "127.0.0.1"}]
    assert R.summary(effective) == "Network isolated · publishes 8069 · internet"
    assert R.short_summary(effective) == "Isolated"
    shown, state, why = R.switch_state([("project", {"network": "isolated"})], {}, "service", "network")
    assert (shown, state) == ("Isolated", "on") and "its own" in why
    off = R.click([("project", {"network": "isolated"})], {}, "service", "network")
    assert off == {"switched_off": ["network"]}
    assert R.switch_state([("project", {"network": "isolated"})], off, "service", "network")[0] == "Off"
    assert R.click([("project", {"network": "isolated"})], off, "service", "network") == {}
    assert R.network_icon("Isolated") == "network-isolated" and R.network_icon("Off") == "network-off"


def test_publications_are_checked_before_the_core_is_asked():
    assert R.pending({"network": "isolated"}, {"network": "isolated", "publish": [{"internal_port": 1}]}) == ("network",)
    found = R.problems({"network": "isolated", "publish": [
        {"internal_port": 8069, "host_port": 18069}, {"internal_port": 8072, "host_port": 18069},
        {"internal_port": 99999}, {"internal_port": 8073, "host_ip": "0.0.0.0", "public": True}]}, "service")
    assert any("published twice" in one for one in found)
    assert any("1 to 65535" in one for one in found)
    assert any("needs Internet on" in one for one in found)


def test_the_window_offers_isolated_and_edits_what_it_lets_through(qapp, dispose, monkeypatch):
    window = R.RestrictionsWindow("App", "/work/shop", "service", {},
                                  outer=[("project", {"files": "read_write"})], outer_name="project")
    try:
        assert window.network.button("Isolated") is not None
        assert window.isolation_field.isHidden()
        window.network.set_current("Isolated")
        assert not window.isolation_field.isHidden() and not window.ports_field.isHidden()
        window.internet.setChecked(True)
        window.publish_add.click()
        window.published.item(0, 0).setText("8069")
        window.published.item(0, 1).setText("18069")
        assert window.value() == {"network": "isolated", "outbound": True,
                                  "publish": [{"internal_port": 8069, "host_port": 18069}]}
        # Other computers: an explicit yes, and only with Internet on.
        monkeypatch.setattr(R.QMessageBox, "question", staticmethod(lambda *args: R.QMessageBox.Yes))
        reach = window.published.cellWidget(0, 2)
        reach.setCurrentIndex(1)
        window._reach_changed(reach, 1)
        assert window.value()["publish"][0] == {"internal_port": 8069, "host_port": 18069,
                                                "host_ip": "0.0.0.0", "public": True}
        window.internet.setChecked(False)
        assert "needs Internet on" in window.problem.text()
    finally:
        dispose(window)


def test_a_project_window_sets_isolation_but_leaves_ports_to_its_services(qapp, dispose):
    window = R.RestrictionsWindow("Shop", "/work/shop", "project", {"network": "isolated", "host_loopback": True})
    try:
        assert not window.isolation_field.isHidden() and window.ports_field.isHidden()
        assert window.this_computer.isChecked()
        assert window.value() == {"network": "isolated", "host_loopback": True}
    finally:
        dispose(window)


def test_isolated_is_offered_greyed_where_this_computer_cannot_do_it(qapp, dispose, monkeypatch):
    monkeypatch.setattr(R, "capabilities", lambda core: {"available": True, "limits": {},
                        "isolated_network": {"available": False, "reason": "This QAVector build has no pasta."}})
    monkeypatch.setattr(R.background, "run", lambda job, done, owner=None: done(job(), None))
    window = R.RestrictionsWindow("App", "/work/shop", "service", {}, core=object())
    try:
        button = window.network.button("Isolated")
        assert not button.isEnabled() and "no pasta" in button.toolTip()
    finally:
        dispose(window)


def test_a_running_isolated_service_shows_its_address_and_takes_no_room_otherwise(qapp, dispose):
    one, two = R.QuickToggles(), R.QuickToggles()
    try:
        for toggles in (one, two):
            toggles.set_model([], {}, "service")
        assert one.set_ports([], False) is False          # nothing changed
        assert two.set_ports([{"internal_port": 8069, "host_port": 18069, "host_ip": "127.0.0.1"},
                              {"internal_port": 8072, "host_port": 18072, "host_ip": "127.0.0.1"}], True)
        assert one.ports.isHidden() and not two.ports.isHidden()
        assert two.ports.text() == ":18069 +1" and "8072 -> localhost:18072" in two.ports.toolTip()
        assert one.sizeHint().width() < two.sizeHint().width()
        # Said again on the next poll, it is not a change to measure for.
        assert two.set_ports([{"internal_port": 8069, "host_port": 18069, "host_ip": "127.0.0.1"},
                              {"internal_port": 8072, "host_port": 18072, "host_ip": "127.0.0.1"}], True) is False
        assert two.set_ports([], False) is True
    finally:
        dispose(one)
        dispose(two)


def test_an_isolated_service_names_its_state_file_and_reads_what_it_publishes(qapp, tmp_path, monkeypatch):
    import json
    import os
    from cms_gui import services
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    supervisor = services.ServiceSupervisor()
    row = sf.RunnerRow("App", "shell", settings={"command": "serve"},
                       extra={"restrictions": {"publish": [{"internal_port": 8069}]}})
    supervisor.sync([sf.ProjectRow("Shop", str(tmp_path), runners=[row],
                                   extra={"restrictions": {"network": "isolated"}})])
    service = supervisor.service("Shop", "App")
    service.runtime_core = _Core()
    started = []
    service._start_attached = lambda argv, cwd, marker=None: started.append(argv)
    try:
        assert service.start()
        request = json.loads(started[0][1].split("=", 1)[1])
        path = request["network_state"]
        assert path == service.network_state() and path.startswith(str(tmp_path / "cache"))
        assert service.publications() == []                     # nothing written yet
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            json.dump({"pid": os.getpid(), "publications": [
                {"internal_port": 8069, "host_port": 18069, "host_ip": "127.0.0.1"}]}, handle)
        assert service.publications()[0]["host_port"] == 18069
        with open(path, "w") as handle:                      # a service long gone says nothing
            json.dump({"pid": 999999999, "publications": [{"internal_port": 1, "host_port": 2}]}, handle)
        assert service.publications() == []
    finally:
        supervisor.dispose()


def test_the_ports_menu_opens_copies_unpublishes_and_publishes_more(qapp, dispose, monkeypatch):
    from cms_gui.pages import services as page_mod
    shown = []

    class Menu(page_mod.QMenu):
        def popup(self, where):
            shown.append([action.text() for action in self.actions() if action.text()])

    monkeypatch.setattr(page_mod, "QMenu", Menu)
    page = page_with(project_restrictions={"network": "isolated"})
    try:
        service = page.supervisor.service("Shop", "App")
        monkeypatch.setattr(service, "publications", lambda: [
            {"internal_port": 8069, "host_port": 18069, "host_ip": "127.0.0.1"}])
        page.ports_menu("Shop", "App", strip(page, "App").restrictions.ports)
        assert shown[0] == ["Open localhost:18069  (service port 8069)", "Copy http://localhost:18069/",
                            "Unpublish localhost:18069", "Publish a port…"]
    finally:
        dispose(page)
