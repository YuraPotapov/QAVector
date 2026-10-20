"""Execution policy belongs to the run; individual approvals bind its fingerprint."""

import json
from urllib.parse import quote

import pytest
from PySide6.QtCore import QEvent, Qt, QUrl
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLineEdit, QVBoxLayout, QWidget

from cms_gui import cycleactivity, cyclereplay, cyclesubjects
from cms_gui.cyclecockpit import ActiveStrip, Cockpit, render
from cms_gui.pages.cycledialogs import NodeDialog
from cms_gui.pages.cycles import CyclesPage, _same_shape
from cms_gui.runner import RunState
from cms_gui.settings import Settings
from test_cycles_page import FakeCore, FakeInventory, SHELL_PLUGIN, graph, payload, row
from test_main_window import window  # noqa: F401 - shared Qt window fixture


def started(mode="important"):
    state = RunState()
    made = graph(("publish", []))
    made["nodes"][0].update(important=True, importance_reason="Pushes a branch")
    state.handle({"kind": "cycle.run.start", "run_id": "r", "cycle": "demo", "graph": made,
                  "execution_policy_version": 1, "initial_execution_mode": mode, "execution_mode": mode,
                  "node_approvals": {}, "policy_changes": [], "control_version": 1})
    return state


def approval(state, status="pending"):
    receipt = {"id": "request&1", "step": "publish", "mode": "important", "status": status,
               "fingerprint": "sha&proof", "attempt": 1, "execution_generation": 2,
               "importance_reason": "Pushes a branch", "label": "Publish <branch>"}
    state.handle({"kind": "cycle.node.approval", "run_id": "r", "receipt": receipt})
    return receipt


def page_for(state=None, default="auto"):
    made = graph(("publish", []))
    made["execution"] = {"default_mode": default}
    made["nodes"][0].update(important=True, importance_reason="Pushes a branch")
    page = CyclesPage(Settings())
    page.set_core(FakeCore({"demo": payload(cycle=made)}))
    page.set_inventory(FakeInventory([row()], [SHELL_PLUGIN]))
    page.open("demo")
    if state:
        page.set_run_state(state)
        page._graph_known(state.cycle["graph"])
    return page


@pytest.mark.parametrize("decision", [True, False])
def test_native_approval_buttons_send_the_bound_decision(qapp, dispose, decision):
    state = started()
    receipt = approval(state)
    assert state.cycle["steps"]["publish"]["status"] == "pending"
    assert state.cycle["execution_status"] == "waiting_for_node_approval"
    assert "Waiting for node approval" in cycleactivity.header(state.cycle, True)
    content = render(state.cycle, True)
    assert "Mode: Important" in content and "Publish &lt;branch&gt;" in content
    assert "Pushes a branch" in content and "approval:?" not in content
    cockpit, sent = Cockpit(), []
    cockpit.approval_answered.connect(lambda *args: sent.append(args))
    cockpit.refresh(state.cycle, True)
    actions = cockpit.approval_actions[receipt["id"]]
    assert cockpit.current.title.text() == receipt["label"]
    assert cockpit.current.state.text() == "Waiting for approval"
    assert actions.approve_button.text() == "Approve" and actions.approve_button.property("variant") == "primary"
    assert actions.cancel_button.text() == "Cancel"
    assert not actions.approve_button.autoDefault() and not actions.cancel_button.autoDefault()
    (actions.approve_button if decision else actions.cancel_button).click()
    assert sent == [(receipt["id"], receipt["fingerprint"], decision)]
    assert not actions.isEnabled()
    cockpit.refresh(state.cycle, True)
    assert cockpit.approval_actions[receipt["id"]] is actions and not actions.isEnabled()
    cockpit.refresh(state.cycle, False)
    assert not cockpit.approval_actions and cockpit.actions.isHidden()
    dispose(cockpit)


def test_parallel_approval_buttons_preserve_other_requests_and_refused_answers(qapp, dispose):
    state = started()
    first = approval(state)
    second = dict(first, id="request2", step="review", label="Review branch", fingerprint="second-proof")
    state.handle({"kind": "cycle.node.approval", "run_id": "r", "receipt": second})
    cockpit, sent = Cockpit(), []
    cockpit.approval_answered.connect(lambda *args: sent.append(args))
    cockpit.refresh(state.cycle, True)
    other = cockpit.approval_actions[second["id"]]
    cockpit.approval_actions[first["id"]].approve_button.click()
    assert other.isEnabled() and sent == [(first["id"], first["fingerprint"], True)]
    approval(state, "approved")
    cockpit.refresh(state.cycle, True)
    assert cockpit.approval_actions == {second["id"]: other}
    other.cancel_button.click()
    assert sent[-1] == (second["id"], second["fingerprint"], False)
    state.cycle["control_error"] = "Decision was refused; review the current request."
    cockpit.refresh(state.cycle, True)
    assert other.isEnabled()
    assert cockpit.actions.height() <= 160
    dispose(cockpit)


@pytest.mark.parametrize("key,modifiers,decision", [
    (Qt.Key_Return, Qt.NoModifier, True), (Qt.Key_Enter, Qt.KeypadModifier, True), (Qt.Key_Escape, Qt.NoModifier, False)])
@pytest.mark.parametrize("focused", ["browser", "approve_button", "cancel_button"])
def test_approval_keyboard_shortcuts_send_one_bound_decision(qapp, dispose, key, modifiers, decision, focused):
    state = started()
    receipt = approval(state)
    cockpit, sent = Cockpit(), []
    cockpit.approval_answered.connect(lambda *args: sent.append(args))
    cockpit.refresh(state.cycle, True)
    cockpit.show()
    cockpit.activateWindow()
    actions = cockpit.approval_actions[receipt["id"]]
    target = cockpit.browser if focused == "browser" else getattr(actions, focused)
    target.setFocus()
    qapp.processEvents()
    assert qapp.focusWidget() is target
    QTest.keyClick(target, key, modifiers)
    assert sent == [(receipt["id"], receipt["fingerprint"], decision)]
    assert not actions.isEnabled()
    cockpit.refresh(state.cycle, True)
    cockpit.browser.setFocus()
    QTest.keyClick(cockpit.browser, key, modifiers)
    assert len(sent) == 1
    dispose(cockpit)


def test_parallel_keyboard_approval_uses_focused_request_and_ignores_auto_repeat(qapp, dispose):
    state = started()
    first = approval(state)
    second = dict(first, id="request2", step="review", label="Review branch", fingerprint="second-proof")
    state.handle({"kind": "cycle.node.approval", "run_id": "r", "receipt": second})
    cockpit, sent = Cockpit(), []
    cockpit.approval_answered.connect(lambda *args: sent.append(args))
    cockpit.refresh(state.cycle, True)
    cockpit.show()
    cockpit.activateWindow()
    cockpit.browser.setFocus()
    qapp.processEvents()
    for key in (Qt.Key_Return, Qt.Key_Escape):
        QTest.keyClick(cockpit.browser, key)
    assert not sent
    actions = cockpit.approval_actions[second["id"]]
    actions.approve_button.setFocus()
    assert cockpit.current.title.text() == second["label"]
    QTest.keyClick(actions.approve_button, Qt.Key_Return)
    assert sent == [(second["id"], second["fingerprint"], True)]
    assert cockpit.approval_actions[first["id"]].isEnabled()
    state.handle({"kind": "cycle.node.approval", "run_id": "r", "receipt": dict(second, status="approved")})
    cockpit.refresh(state.cycle, True)
    cockpit.browser.setFocus()
    qapp.sendEvent(cockpit.browser, QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier, "\r", True))
    qapp.sendEvent(cockpit.browser, QKeyEvent(QEvent.KeyRelease, Qt.Key_Return, Qt.NoModifier, "\r", True))
    assert len(sent) == 1
    QTest.keyClick(cockpit.browser, Qt.Key_Escape)
    assert sent[-1] == (first["id"], first["fingerprint"], False)
    dispose(cockpit)


def test_approval_shortcuts_stay_inside_the_live_cockpit(qapp, dispose):
    state = started()
    receipt = approval(state)
    host, cockpit, field, sent = QWidget(), Cockpit(), QLineEdit(), []
    layout = QVBoxLayout(host)
    layout.addWidget(cockpit)
    layout.addWidget(field)
    cockpit.approval_answered.connect(lambda *args: sent.append(args))
    cockpit.refresh(state.cycle, True)
    host.show()
    host.activateWindow()
    field.setFocus()
    qapp.processEvents()
    for key in (Qt.Key_Return, Qt.Key_Escape):
        QTest.keyClick(field, key)
    assert not sent
    cockpit.refresh(state.cycle, False)
    cockpit.browser.setFocus()
    for key in (Qt.Key_Return, Qt.Key_Escape):
        QTest.keyClick(cockpit.browser, key)
    assert not sent and not cockpit.approval_actions
    cockpit.refresh(state.cycle, True)
    cockpit.hide()
    field.setFocus()
    for key in (Qt.Key_Return, Qt.Key_Escape):
        QTest.keyClick(field, key)
    assert not sent
    cockpit.show()
    cockpit.browser.setFocus()
    QTest.keyClick(cockpit.browser, Qt.Key_Return)
    assert sent == [(receipt["id"], receipt["fingerprint"], True)]
    dispose(host)


@pytest.mark.parametrize("navigation", ["open", "view", "context"])
def test_opening_cockpit_places_keyboard_focus_on_the_approval(qapp, dispose, navigation):
    state = started()
    receipt = approval(state)
    page, sent = page_for(state), []
    page.approval_answered.connect(lambda *args: sent.append(args))
    page.show()
    page.activateWindow()
    page.run_button.setFocus()
    qapp.processEvents()
    if navigation == "view":
        page.run_view.set_current("Graph")
        QTest.mouseClick(page.run_view.button("Cockpit"), Qt.LeftButton)
    elif navigation == "context":
        page.context_switch.set_current("Design")
        QTest.mouseClick(page.context_switch.button("Run"), Qt.LeftButton)
    else:
        page.show_cockpit()
    qapp.processEvents()
    assert qapp.focusWidget() is page.cockpit.browser
    QTest.keyClick(page.cockpit.browser, Qt.Key_Return)
    assert sent == [(receipt["id"], receipt["fingerprint"], True)]
    dispose(page)


def test_old_or_wrong_run_approval_events_do_not_create_phantom_requests():
    state = started()
    state.handle({"kind": "cycle.node.approval", "run_id": "other", "receipt": {"id": "bad", "status": "pending"}})
    assert state.cycle["node_approvals"] == {}
    approval(state)
    approval(state, "invalidated")
    assert "Waiting for node approval" not in cycleactivity.header(state.cycle, True)


def test_approval_preview_escapes_inputs_and_displays_bounded_redacted_evidence():
    state = started()
    receipt = approval(state)
    receipt.update(purpose="Push <branch> & update server", timeout=30,
        resolved_inputs={"command": "echo <script> & value", "token": "[REDACTED]", "long": "x" * 6500},
        declared_effects=["Push <remote>"], permissions=["process.spawn", "network&remote"])
    state.handle({"kind": "cycle.node.approval", "run_id": "r", "receipt": receipt})
    content = render(state.cycle, True)
    assert "Push &lt;branch&gt; &amp; update server" in content
    assert "echo &lt;script&gt; &amp; value" in content and "<script>" not in content
    assert "Declared changes:</b> Push &lt;remote&gt;" in content
    assert "Technical details" in content and "Resolved inputs" not in content
    assert "[REDACTED]" not in content and "process.spawn" not in content and "Timeout:" not in content
    expanded = {(receipt["id"], receipt["fingerprint"])}
    content = render(state.cycle, True, expanded_details=expanded)
    assert "[REDACTED]" in content and "Preview truncated" in content
    assert "Timeout: 30s" in content and "Effects (declared): Push &lt;remote&gt;" in content
    assert "Declared capabilities: process.spawn, network&amp;remote" in content
    receipt.update(timeout=None, declared_effects=[])
    state.handle({"kind": "cycle.node.approval", "run_id": "r", "receipt": receipt})
    content = render(state.cycle, True, expanded_details=expanded)
    assert "No timeout configured" in content and "Effects not recorded" in content


def jira_approval(state):
    receipt = approval(state)
    receipt.update(plugin="jira.issues", mode=state.cycle["execution_mode"],
        label="Find release tasks", importance_reason="Review the release queue",
        purpose="Read a person's issues and the comments on them. Reads only.",
        resolved_inputs={"attachment_bytes": 25000000, "attachments": "images", "auth": "basic",
            "comment_limit": 20, "comments": True, "description_limit": 20000,
            "email": "reader@example.test", "flavour": "cloud", "issue": "",
            "jql": 'status = "READY FOR PROD (RELEASE)"', "order": "updated ASC", "project": "CLAIM",
            "role": "everyone", "site": "https://example.atlassian.net", "token": "[redacted]", "verify_tls": True})
    state.handle({"kind": "cycle.node.approval", "run_id": "r", "receipt": receipt})
    return receipt


def test_jira_approval_explains_search_without_connection_settings_or_invented_defaults():
    state = started("approve")
    receipt = jira_approval(state)
    content = render(state.cycle, True)
    for expected in ("Read Jira issues.", "Jira access:</b> Read only", "Projects:</b> CLAIM",
                     "Search conditions:</b> status = &quot;READY FOR PROD (RELEASE)&quot;",
                     "Sort by:</b> updated ASC", "Include:</b> Comments, Images", "Whose issues:</b> Everyone"):
        assert expected in content
    for hidden in ("attachment_bytes", "25000000", "comment_limit", "description_limit", "reader@example.test",
                   "basic", "cloud", "verify_tls", "[redacted]", "Resolved inputs", "Issue limit"):
        assert hidden not in content
    receipt["resolved_inputs"].update(issue="QA-42", limit=1, status="Open", user="someone")
    state.handle({"kind": "cycle.node.approval", "run_id": "r", "receipt": receipt})
    content = render(state.cycle, True)
    assert "Issue:</b> QA-42" in content
    for inactive in ("Search conditions", "READY FOR PROD", "Sort by", "Whose issues", "Statuses", "Issue limit", "Projects:"):
        assert inactive not in content


def test_technical_disclosure_is_local_persists_on_refresh_and_resets_for_new_bound_inputs(qapp, dispose):
    state = started("approve")
    receipt = jira_approval(state)
    cockpit, sent, selected = Cockpit(), [], []
    cockpit.approval_answered.connect(lambda *args: sent.append(args))
    cockpit.node_selected.connect(selected.append)
    cockpit.refresh(state.cycle, True)
    actions = cockpit.approval_actions[receipt["id"]]
    details = QUrl("details:" + quote(receipt["id"], safe=""))
    assert "reader@example.test" not in cockpit.browser.toPlainText()
    cockpit.browser.anchorClicked.emit(details)
    assert "reader@example.test" in cockpit.browser.toPlainText()
    assert "[redacted]" in cockpit.browser.toPlainText()
    assert not sent and not selected and actions.isEnabled()
    cockpit.refresh(state.cycle, True)
    assert "reader@example.test" in cockpit.browser.toPlainText()
    assert cockpit.approval_actions[receipt["id"]] is actions
    cockpit.browser.anchorClicked.emit(details)
    assert "reader@example.test" not in cockpit.browser.toPlainText()
    cockpit.browser.anchorClicked.emit(details)
    receipt.update(fingerprint="changed-inputs")
    state.handle({"kind": "cycle.node.approval", "run_id": "r", "receipt": receipt})
    cockpit.refresh(state.cycle, True)
    assert "reader@example.test" not in cockpit.browser.toPlainText()
    cockpit.browser.anchorClicked.emit(details)
    assert "reader@example.test" in cockpit.browser.toPlainText()
    cockpit.refresh(dict(state.cycle, run_id="different-run"), True)
    assert "reader@example.test" not in cockpit.browser.toPlainText()
    cockpit.refresh(state.cycle, False)
    cockpit.browser.anchorClicked.emit(details)
    assert "reader@example.test" in cockpit.browser.toPlainText()
    assert not cockpit.approval_actions and not sent
    dispose(cockpit)


def test_auto_explains_important_steps_are_automatic(qapp, dispose):
    state = started("auto")
    assert "Important steps also run without implicit approval" in render(state.cycle, True)
    page = page_for(default="auto")
    assert "Important steps also run without implicit approval" in page._mode_preflight()
    dispose(page)


def test_policy_changes_update_effective_mode_and_keep_initial_mode():
    state = started()
    change = {"id": 1, "from": "important", "to": "approve", "reason": "paused_change"}
    state.handle({"kind": "cycle.policy.changed", "run_id": "r", "execution_mode": "approve", "receipt": change})
    assert state.cycle["initial_execution_mode"] == "important"
    assert state.cycle["execution_mode"] == "approve"
    assert state.cycle["policy_changes"] == [change]
    assert "important to approve" in render(state.cycle, True)


def test_subjects_explain_node_approval_and_effective_mode():
    session = {"status": "waiting", "running": True, "execution_status": "waiting_for_node_approval", "execution_mode": "approve"}
    assert cyclesubjects.status_word(session) == "waiting for node approval"
    assert "Mode: Approve" in cyclesubjects.details_html(session)


def test_selector_defaults_resume_saved_mode_and_changes_live_only_when_paused(qapp, dispose):
    page = page_for(default="approve")
    assert page.requested_execution_mode() == "approve"
    page.execution_mode.setCurrentIndex(page.execution_mode.findData("auto"))
    page._refresh_cockpit()
    assert page.requested_execution_mode() == "auto"
    state = started("important")
    page.set_run_state(state)
    page._refresh_cockpit()
    assert page.requested_execution_mode() == "important"
    assert not page.execution_mode.isEnabled()
    state.handle({"kind": "cycle.run.state", "run_id": "r", "paused": True})
    page._refresh_cockpit()
    assert page.execution_mode.isEnabled()
    sent = []
    page.control_requested.connect(lambda *args: sent.append(args))
    page.execution_mode.setCurrentIndex(page.execution_mode.findData("approve"))
    page._execution_mode_changed(2)
    assert sent == [("set_mode", "approve")]
    state.cycle_running = False
    page._refresh_cockpit()
    assert page.requested_execution_mode() == "important"
    dispose(page)


def test_important_reason_is_available_before_run_and_in_graph_details(qapp, dispose):
    page = page_for(default="important")
    assert "Pushes a branch" in page._mode_preflight()
    node = page.canvas.node("publish")
    assert "Important: Pushes a branch" in node.toolTip()
    original = graph(("publish", []))
    changed = graph(("publish", []))
    changed["nodes"][0]["important"] = True
    assert not _same_shape(original, changed)
    dispose(page)


def test_node_dialog_can_mark_and_unmark_importance_without_losing_other_data(qapp, dispose):
    step = {"id": "publish", "plugin": "command.shell", "with": {"command": "true"}, "extra": "kept"}
    dialog = NodeDialog(step, SHELL_PLUGIN)
    dialog._widgets["important"].setChecked(True)
    dialog._widgets["importance_reason"].setText("Publishes work")
    dialog._save()
    assert dialog.saved["important"] and dialog.saved["importance_reason"] == "Publishes work"
    again = NodeDialog(dialog.saved, SHELL_PLUGIN)
    again._widgets["important"].setChecked(False)
    again._widgets["importance_reason"].clear()
    again._save()
    assert "important" not in again.saved and "importance_reason" not in again.saved
    assert again.saved["extra"] == "kept"
    dispose(dialog)
    dispose(again)


def test_strip_keeps_policy_visible_away_from_cycles_page(qapp, dispose):
    state = started("approve")
    strip = ActiveStrip(state, Settings())
    approval(state)
    strip.refresh()
    assert "Mode: Approve" in strip.summary.text()
    assert "Waiting for node approval" in strip.summary.text()
    dispose(strip)


@pytest.mark.parametrize("with_metadata", [True, False])
def test_canonical_policy_replay_wins_over_stale_events_and_metadata(tmp_path, with_metadata):
    execution = tmp_path / ".execution"
    execution.mkdir()
    state = started("auto")
    receipt = approval(state)
    (execution / "execution-policy.json").write_text(json.dumps({"schema": 1, "run_id": "r",
        "initial_execution_mode": "auto", "execution_mode": "approve", "execution_policy_version": 2,
        "node_approvals": {receipt["id"]: dict(receipt, status="invalidated")},
        "policy_changes": [{"id": 1, "from": "auto", "to": "approve"}]}))
    if with_metadata:
        (tmp_path / "metadata.json").write_text(json.dumps({"id": "r", "execution_mode": "important", "steps": {}}))
    restored = RunState()
    assert cyclereplay.restore(str(tmp_path), restored, events=[{"kind": "cycle.run.start", "run_id": "r", "cycle": "demo",
        "graph": state.cycle["graph"], "execution_mode": "auto"}])
    assert restored.cycle["execution_mode"] == "approve"
    assert restored.cycle["initial_execution_mode"] == "auto"
    assert restored.cycle["node_approvals"][receipt["id"]]["status"] == "invalidated"


def test_mode_and_approval_commands_are_bound_to_existing_run(window, monkeypatch):
    sent = []
    monkeypatch.setattr(window.process, "send_command", lambda **message: sent.append(message) or True)
    state = started()
    window.run_state.cycle, window.run_state.cycle_running = state.cycle, True
    receipt = approval(window.run_state)
    window._answer_cycle_node_approval(receipt["id"], "wrong", True)
    assert not sent
    window._answer_cycle_node_approval(receipt["id"], receipt["fingerprint"], False)
    assert sent[-1] == {"command": "cycle.node.approval.answer", "run_id": "r", "request_id": receipt["id"],
                        "fingerprint": receipt["fingerprint"], "approved": False}
    window._control_cycle("set_mode", "approve")
    assert len(sent) == 1
    window.run_state.cycle["paused"] = True
    window._control_cycle("set_mode", "approve")
    assert sent[-1] == {"command": "cycle.mode.change", "run_id": "r", "mode": "approve"}
    window.run_state.cycle_running = False


def test_resume_passes_selected_policy_to_core(window, monkeypatch):
    sent = []
    monkeypatch.setattr(window.process, "is_running", lambda: False)
    monkeypatch.setattr(window.process, "start", lambda argv, **kw: sent.append(argv))
    monkeypatch.setattr(window.core, "is_configured", lambda: True)
    monkeypatch.setattr(window.core, "argv", lambda *args: list(args))
    window.start_cycle_run("demo", "resume", "saved-run", execution_mode="approve")
    assert "--cycle-resume=saved-run" in sent[0] and "--cycle-mode=approve" in sent[0]


@pytest.mark.parametrize("how,selected,default,sent", [
    ("", "auto", "auto", None),
    ("", "important", "important", None),
    ("", "approve", "auto", "approve"),
    ("from", "auto", "important", "auto"),
    ("resume", "auto", "auto", "auto"),
])
def test_page_run_sends_mode_only_when_it_is_not_the_cycle_default(window, monkeypatch, how, selected, default, sent):
    # An older core refuses --cycle-mode, so an ordinary Run must not need it.
    calls = []
    monkeypatch.setattr(window.cycles, "requested_execution_mode", lambda: selected)
    monkeypatch.setattr(window.cycles, "default_execution_mode", lambda: default)
    monkeypatch.setattr(window, "start_cycle_run", lambda *args, **kwargs: calls.append(kwargs.get("execution_mode")))
    window._start_page_cycle_run("demo", how, "step" if how else "")
    assert calls == [sent]


def test_default_execution_mode_follows_the_open_cycle(qapp, dispose):
    page = page_for(default="important")
    assert page.default_execution_mode() == "important"
    dispose(page)
