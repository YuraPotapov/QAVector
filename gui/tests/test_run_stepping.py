"""Stepping a paused scenario from the Run page (engine/stepping.py).

The core does the stepping; these check that its events become the right
picture - a paused window, the step looked at, the breakpoints - and that the
controls ask for the right thing. Nothing here talks to a launcher.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent

from cms_gui import launch
from cms_gui.pages import runsteps
from cms_gui.pages.run import GUTTER_PX, RunPage, SessionPanel, _StepRow
from cms_gui.runner import RunState

TREE = {"id": "0", "label": "Order", "kind": "group", "children": [
    {"id": "0/%d" % i, "label": "step %d" % (i + 1), "kind": "step", "step_index": i}
    for i in range(4)]}


def _run(state, name="shop-alice", live=True):
    for event in ({"kind": "session.start", "scenarios": ["order"]},
                  {"kind": "flow.start", "scenario": "Order", "id": "order", "tree": TREE,
                   "steps": 4},
                  {"kind": "step.setup", "scenario": "order", "live": live,
                   "breakpoints": []}):
        state.handle(dict(event, session=name))
    for index in range(3):
        state.handle({"kind": "step.end", "session": name, "index": index,
                      "status": "pass"})
    return state.sessions[name]


def _paused(state, cursor=3, last=3, reason="pause", view=None, name="shop-alice"):
    state.handle({"kind": "step.paused", "session": name, "cursor": cursor, "last": last,
                  "reason": reason, "view": view})


# -- the model ----------------------------------------------------------------------
def test_a_paused_window_says_so_and_where_it_is():
    state = RunState()
    session = _run(state)
    _paused(state, cursor=1, last=3, reason="breakpoint", view="/tmp/x/step_001.html")
    assert session["state"] == "paused"
    assert session["step"]["cursor"] == 1 and session["step"]["view"].endswith("001.html")
    assert session in state.live()                 # Stop still offers it
    state.handle({"kind": "step.resumed", "session": "shop-alice", "cursor": 1})
    assert session["state"] == "running"
    assert session["step"]["paused"] is False and session["step"]["last"] == 1


def test_steps_taken_back_are_not_counted_as_done_any_more():
    state = RunState()
    session = _run(state)
    state.handle({"kind": "steps.rewound", "session": "shop-alice", "first": 1, "last": 3})
    assert sorted(session["steps"]) == [0] and session["done"] == 1
    run = session["runs"]["order"]
    assert run["done"] == 1 and run["rewound"] == 1


def test_breakpoints_belong_to_their_scenario_s_record():
    state = RunState()
    session = _run(state)
    state.handle({"kind": "step.setup", "session": "shop-alice", "scenario": "order",
                  "live": True, "breakpoints": [3, 1]})
    assert session["runs"]["order"]["breakpoints"] == [1, 3]


def test_a_finished_scenario_is_not_paused_any_more():
    state = RunState()
    session = _run(state)
    _paused(state)
    state.handle({"kind": "flow.end", "session": "shop-alice", "status": "pass",
                  "passed": 4, "total": 4})
    assert session["step"]["paused"] is False and session["state"] == "passed"


# -- the words ----------------------------------------------------------------------
def test_the_bar_says_the_same_as_the_overlay_in_the_window():
    assert runsteps.describe({"paused": False}) == (
        "Running", "Pause stops it after the step it is on")
    assert runsteps.describe({"paused": True, "cursor": 0, "last": 0}, 4) == (
        "Paused before step 1 of 4", "")
    assert runsteps.describe({"paused": True, "cursor": 3, "last": 3,
                              "reason": "breakpoint"}, 4) == (
        "Paused after step 3 of 4", "At a breakpoint")
    assert runsteps.describe({"paused": True, "cursor": 1, "last": 3}, 4) == (
        "The page after step 1", "Continue runs step 2 again, on the live page")
    assert runsteps.describe({"paused": True, "cursor": 2, "last": 2,
                              "reason": "failure"}, 4)[0] == "Step 2 of 4 failed"


# -- the controls -------------------------------------------------------------------
def test_the_bar_asks_to_continue_a_paused_window_and_to_pause_a_running_one(qapp):
    bar = runsteps.StepBar()
    asked = []
    bar.asked.connect(asked.append)
    bar.show_state({"paused": False}, 4, {"pause": "F8"})
    bar.buttons["pause"].click()
    assert not bar.buttons["forward"].isEnabled()
    bar.show_state({"paused": True, "cursor": 0, "last": 0}, 4, {"pause": "F8"})
    bar.buttons["pause"].click()
    assert not bar.buttons["back"].isEnabled()      # nothing before the first step
    assert asked == [{"command": "pause"}, {"command": "continue"}]
    assert bar.buttons["pause"].toolTip() == "Continue (F8)"


def _panel(state, qapp):
    panel = SessionPanel()
    asked = []
    panel.step_asked.connect(lambda name, command: asked.append(command))
    panel.update_from(state.sessions["shop-alice"])
    return panel, asked


def _click(row, x):
    point = QPointF(x, row.height() / 2)
    event = QMouseEvent(QMouseEvent.MouseButtonPress, point, point, Qt.LeftButton,
                        Qt.LeftButton, Qt.NoModifier)
    row.mousePressEvent(event)


def test_a_run_nobody_can_pause_looks_as_it_did(qapp):
    state = RunState()
    _run(state, live=False)
    panel, _asked = _panel(state, qapp)
    assert panel.step_bar.isHidden()
    assert all(row.gutter == 0 for row in panel.steps.findChildren(_StepRow))


def test_a_click_in_the_gutter_asks_for_a_breakpoint_and_again_takes_it_away(qapp):
    state = RunState()
    _run(state)
    panel, asked = _panel(state, qapp)
    assert not panel.step_bar.isHidden()
    rows = panel.steps.findChildren(_StepRow)
    _click(rows[2], GUTTER_PX / 2)
    _click(rows[2], GUTTER_PX / 2)
    assert asked == [{"command": "breakpoints", "scenario": "order", "indices": [2]},
                     {"command": "breakpoints", "scenario": "order", "indices": []}]


def test_a_click_on_a_step_taken_goes_to_its_page_only_while_paused(qapp):
    state = RunState()
    _run(state)
    panel, asked = _panel(state, qapp)
    rows = panel.steps.findChildren(_StepRow)
    _click(rows[0], GUTTER_PX + 40)
    assert asked == []                               # running: nothing to look at
    _paused(state)
    panel.update_from(state.sessions["shop-alice"])
    _click(rows[0], GUTTER_PX + 40)
    _click(rows[3], GUTTER_PX + 40)                  # not taken yet
    assert asked == [{"command": "cursor", "index": 1}]


def test_the_step_looked_at_and_the_one_to_run_next_are_marked(qapp):
    state = RunState()
    _run(state)
    _paused(state, cursor=1, last=3)
    panel, _asked = _panel(state, qapp)
    rows = panel.steps.findChildren(_StepRow)
    assert [row.property("mark") or "" for row in rows] == ["cursor", "", "", ""]
    _paused(state, cursor=3, last=3)
    panel.update_from(state.sessions["shop-alice"])
    assert [row.property("mark") or "" for row in rows] == ["", "", "", "next"]


# -- the page --------------------------------------------------------------------------
def test_the_keys_go_to_the_paused_window(qapp, dispose):
    state = RunState()
    page = RunPage(state)
    _run(state, "shop-alice")
    _run(state, "shop-bob")
    _paused(state, name="shop-bob")
    page.refresh()
    asked = []
    page.step_asked.connect(lambda name, command: asked.append((name, command)))
    assert page.step("pause") and page.step("back")
    assert asked == [("shop-bob", {"command": "continue"}), ("shop-bob", {"command": "back"})]


def test_without_a_window_to_step_the_keys_do_nothing(qapp, dispose):
    state = RunState()
    page = RunPage(state)
    _run(state, live=False)
    page.refresh()
    assert page.step("pause") is False


def test_the_copy_pane_is_there_while_paused_and_says_where_the_live_page_is(qapp, dispose):
    state = RunState()
    page = RunPage(state)
    _run(state)
    page.refresh()
    assert page.copy_pane.isHidden()
    _paused(state)
    page.refresh()
    assert not page.copy_pane.isHidden()
    assert page.copy_pane.title.text() == "The live page"
    assert page.copy_pane._empty_title.text() == "The page is in the browser window"


def test_a_step_with_no_copy_says_why_rather_than_showing_nothing(qapp, dispose):
    state = RunState()
    page = RunPage(state)
    _run(state)
    _paused(state, cursor=1, last=3, view=None)
    page.refresh()
    assert page.copy_pane.title.text() == "The page after step 1"
    assert page.copy_pane._empty_title.text() == "No copy of this step"
    assert page.copy_pane.preview is None            # no web view started for nothing


# -- the launch options -------------------------------------------------------------------
def test_live_history_is_kept_and_failures_do_not_pause_unless_asked():
    state = launch.to_command_state(launch.DEFAULTS)
    assert state["--step-history"] is True and state["--pause-on-failure"] is False
    config = launch.merged({"sessions": {"pause_on_failure": True, "step_history": False}})
    state = launch.to_command_state(config)
    assert state["--step-history"] is False and state["--pause-on-failure"] is True
