"""The Cycles page: what it draws, what it says, and what it sends.

The page owns no file format and knows nothing about what a plugin does - the
core owns both - so what is worth testing here is the boundary: that a cycle is
drawn whole when it is opened, that a live run repaints nodes rather than
rebuilding the scene, and that the Inspector is built from the metadata the core
published rather than from a second list kept here.

The core is faked throughout. Its real behaviour is covered by
tests/test_cycle_*.py and tests/test_session_launcher.py in the core checkout;
spawning a subprocess here would make these slow and prove nothing extra.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cms_gui import core as core_mod
from cms_gui.pages.cycles import (ROLE_ID, TAB_OUTPUT, TAB_PROBLEMS,
                                  TAB_STAGES, TAB_YAML, CyclesPage)
from cms_gui.runner import RunState
from cms_gui.settings import Settings


def graph(*steps):
    """``("a", []), ("b", ["a"])`` -> the payload cycle.model.to_graph makes."""
    layers, nodes, edges = {}, [], []
    for name, needs in steps:
        layer = 1 + max([layers[one] for one in needs], default=-1)
        layers[name] = layer
        nodes.append({"id": name, "label": name.title(), "plugin":
                      "command.shell", "needs": list(needs), "layer": layer,
                      "row": sum(1 for one in nodes if one["layer"] == layer),
                      "disabled": False, "condition": "", "timeout": None,
                      "retry": 1, "on_failure": "stop"})
        edges.extend({"from": one, "to": name, "kind": "dependency"}
                     for one in needs)
    return {"id": "demo", "name": "Demo cycle", "description": "",
            "version": 1, "variables": {}, "nodes": nodes, "edges": edges}


DIAMOND = graph(("a", []), ("b", ["a"]), ("c", ["a"]), ("d", ["b", "c"]))


class FakeCore:
    """Records what the page asked for, and answers with what it was given."""

    def __init__(self, cycles=None):
        self.cycles = cycles or {}
        self.shown = []
        self.saved = []
        self.deleted = []
        self.raise_on_show = None
        # The secrets store, as the page sees it: names in, values never out.
        self.stored = {}
        self.secret_writes = []
        self.secret_deletes = []
        # What --cycle-sessions answers, and every session somebody deleted.
        self.sessions = []
        self.sessions_asked = 0
        self.sessions_deleted = []

    def cycle_sessions(self, cycle_id=""):
        self.sessions_asked += 1
        return {"ok": True, "sessions": list(self.sessions)}

    def cycle_watch_planned(self):
        return {"ok": True, "planned": list(getattr(self, "planned", []))}

    def cycle_plan_remove(self, entry_id):
        self.planned = [one for one in getattr(self, "planned", [])
                        if one.get("id") != entry_id]
        return {"ok": True, "removed": True}

    def cycle_session_delete(self, session_id):
        self.sessions_deleted.append(session_id)
        self.sessions = [one for one in self.sessions if one.get("id") != session_id]
        return {"ok": True, "id": session_id, "runs": [], "memory": ""}

    def cycle_show(self, cycle_id):
        self.shown.append(cycle_id)
        if self.raise_on_show:
            raise core_mod.CoreError(self.raise_on_show)
        return self.cycles[cycle_id]

    def cycle_save(self, cycle_id, document):
        self.saved.append((cycle_id, document))
        return {"ok": True, "id": cycle_id, "problems": []}

    def cycle_delete(self, cycle_id):
        self.deleted.append(cycle_id)
        return {"ok": True, "id": cycle_id, "problems": []}

    def cycle_secrets(self, cycle_id):
        return {"ok": True, "cycle": cycle_id, "problems": [],
                "secrets": sorted(self.stored.get(cycle_id, {}))}

    def cycle_secret_set(self, cycle_id, name, value):
        self.secret_writes.append((cycle_id, name, value))
        self.stored.setdefault(cycle_id, {})[name] = value
        return {"ok": True, "problems": []}

    def cycle_secret_delete(self, cycle_id, name=None):
        self.secret_deletes.append((cycle_id, name))
        if name:
            self.stored.get(cycle_id, {}).pop(name, None)
        else:
            self.stored.pop(cycle_id, None)
        return {"ok": True, "problems": []}


class FakeInventory:
    def __init__(self, cycles=(), plugins=()):
        self.cycles = list(cycles)
        self.cycle_plugins = list(plugins)

    def cycle_plugin(self, plugin_id):
        for entry in self.cycle_plugins:
            if entry.get("id") == plugin_id:
                return entry
        return {}


def payload(cycle_id="demo", cycle=None, writable=True, problems=(),
            project="", document=None):
    graph = DIAMOND if cycle is None else cycle
    return {"id": cycle_id, "path": "/cycles/%s.yaml" % cycle_id,
            "writable": writable, "source": "user" if writable else "bundled",
            "yaml": "id: %s\nsteps: []\n" % cycle_id,
            "cycle": graph,
            # What the core hands over for editing - see cycle.model.to_document.
            "document": document if document is not None else {
                "id": cycle_id, "name": graph.get("name", ""),
                "project": project,
                "steps": [{"id": node["id"], "plugin": node["plugin"]}
                          for node in graph.get("nodes") or []]},
            "problems": list(problems)}


def row(cycle_id="demo", name="Demo cycle", problems=(), project="",
        writable=True):
    return {"id": cycle_id, "name": name, "description": "", "steps": 4,
            "project": project, "writable": writable, "source": "user",
            "problems": list(problems)}


def projects(*names):
    """The rows the projects file would have given the page."""
    from cms_gui import cycleprojectsfile as cpf
    return [cpf.ProjectRow(name=name) for name in names]


SHELL_PLUGIN = {"id": "command.shell", "name": "Shell Command",
                "category": "action", "summary": "Run a command.",
                "inputs": [{"key": "command", "label": "Command",
                            "kind": "multiline", "hint": "", "required": True,
                            "default": None, "options": []}],
                "outputs": [{"key": "exit_code", "type": "number", "hint": ""}],
                "permissions": ["process.spawn"], "background": False,
                "concurrency_group": ""}


@pytest.fixture(autouse=True)
def agree(monkeypatch):
    """Answer the run confirmations with Yes unless a test says otherwise.

    Running the whole cycle, and running from a step onwards, both ask first -
    they spend agent calls and move a Jira issue, and neither is undone by
    pressing Stop. A test that left the question unanswered would hang on a
    modal dialog, so the default here is the one that lets the test get on
    with what it is actually about.
    """
    from PySide6.QtWidgets import QMessageBox

    from cms_gui import widgets

    monkeypatch.setattr(widgets, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.Yes))


def settled(*_ignored):
    """Let a coalesced repaint happen - see CyclesPage._schedule_paint.

    A run reports several times a second and the page answers by asking for one
    repaint per turn of the event loop rather than doing one per event, so a
    test that pushes events in and looks straight away is looking too early.
    """
    from PySide6.QtWidgets import QApplication

    QApplication.processEvents()


@pytest.fixture
def page(qapp, dispose):
    made = CyclesPage(Settings())
    yield made
    dispose(made)


@pytest.fixture
def opened(page):
    """A page with one cycle read into it, which is most tests' starting point."""
    page.set_core(FakeCore({"demo": payload()}))
    page.set_inventory(FakeInventory([row()], [SHELL_PLUGIN]))
    page.open("demo")
    return page


# ------------------------------------------------------------------ explorer
def test_the_explorer_lists_what_the_inventory_carries(page):
    page.set_core(FakeCore())
    page.set_inventory(FakeInventory([row("nightly", "Nightly"),
                                      row("release", "Release")]))
    assert sorted(page.explorer._rows) == ["nightly", "release"]


def test_a_cycle_sits_under_the_project_it_names(page):
    page._projects = projects("Portal")
    page.set_core(FakeCore())
    page.set_inventory(FakeInventory([row(project="Portal")]))

    root = page.explorer.tree.topLevelItem(0)
    assert root.text(0) == "Portal"
    assert root.child(0).data(0, ROLE_ID) == "demo"


def test_a_cycle_naming_no_project_shows_as_unassigned(page):
    page._projects = projects("Portal")
    page.set_core(FakeCore())
    page.set_inventory(FakeInventory([row(project="")]))

    names = [page.explorer.tree.topLevelItem(index).text(0)
             for index in range(page.explorer.tree.topLevelItemCount())]
    assert names == ["Portal", "Unassigned"]


def test_a_cycle_naming_a_project_nobody_knows_is_not_lost(page):
    """Losing sight of a cycle because its project was renamed would be the
    worst possible answer."""
    page._projects = projects("Portal")
    page.set_core(FakeCore())
    page.set_inventory(FakeInventory([row(project="Gone")]))

    assert "demo" in page.explorer._rows
    assert page.explorer._rows["demo"].parent().text(0) == "Unassigned"


def test_an_empty_unassigned_group_is_not_shown_at_all(page):
    """A group holding nothing is a question nobody asked."""
    page._projects = projects("Portal")
    page.set_core(FakeCore())
    page.set_inventory(FakeInventory([row(project="Portal")]))

    names = [page.explorer.tree.topLevelItem(index).text(0)
             for index in range(page.explorer.tree.topLevelItemCount())]
    assert names == ["Portal"]


def test_a_project_with_no_cycles_still_shows(page):
    """It is where the next one goes."""
    page._projects = projects("Portal", "Empty")
    page.set_core(FakeCore())
    page.set_inventory(FakeInventory([row(project="Portal")]))

    names = [page.explorer.tree.topLevelItem(index).text(0)
             for index in range(page.explorer.tree.topLevelItemCount())]
    assert names == ["Portal", "Empty"]


def test_an_empty_inventory_says_so_rather_than_showing_nothing(page):
    page.set_core(FakeCore())
    page.set_inventory(FakeInventory([]))
    assert page.explorer._rows == {}
    assert "0 cycle" in page.explorer.count.text()


def test_a_cycle_with_problems_is_marked_in_the_explorer(page):
    page.set_core(FakeCore())
    page.set_inventory(FakeInventory([row(problems=["b: needs 'ghost'"])]))
    item = page.explorer._rows["demo"]
    assert not item.icon(0).isNull()
    assert "ghost" in item.toolTip(0)


def test_searching_hides_what_does_not_match(page):
    page.set_core(FakeCore())
    page.set_inventory(FakeInventory([row("nightly", "Nightly"),
                                      row("release", "Release")]))
    page.explorer.search.setText("night")
    assert not page.explorer._rows["nightly"].isHidden()
    assert page.explorer._rows["release"].isHidden()


def test_clearing_the_search_shows_them_all_again(page):
    page.set_core(FakeCore())
    page.set_inventory(FakeInventory([row("nightly"), row("release")]))
    page.explorer.search.setText("night")
    page.explorer.search.setText("")
    assert not any(item.isHidden() for item in page.explorer._rows.values())


# -------------------------------------------------------------------- opening
def test_opening_a_cycle_asks_the_core_for_it(opened):
    assert opened.core.shown == ["demo"]


def test_opening_a_cycle_draws_all_of_it_at_once(opened):
    """The whole point of drawing a cycle rather than listing it."""
    assert sorted(opened.canvas.node_ids()) == ["a", "b", "c", "d"]
    assert len(opened.canvas._edges) == 4


def test_the_page_is_headed_with_the_cycle_s_name(opened):
    assert opened.title.text() == "Demo cycle"


def test_a_long_name_does_not_widen_the_page(page, qapp):
    """The header shortens its text instead: a page wider than the screen
    holds the whole window wider than it, and then it cannot be maximized."""
    page.show()     # a hidden layout never recomputes its minimum
    qapp.processEvents()
    short = page.minimumSizeHint().width()
    page.title.setText("A cycle with a very long name " * 6)
    page.state.setText("28 step(s), 1 problem(s) - working on task QA-123 " * 3)
    qapp.processEvents()
    assert page.minimumSizeHint().width() == short
    assert page.title.text().startswith("A cycle with a very long name")


def test_the_page_says_how_big_the_cycle_is(opened):
    assert "4 step" in opened.state.text()


def test_a_cycle_that_ships_with_the_app_says_so(page):
    page.set_core(FakeCore({"demo": payload(writable=False)}))
    page.set_inventory(FakeInventory([row()]))
    page.open("demo")
    assert "ships with the app" in page.state.text()


def test_the_problems_of_an_open_cycle_are_shown(page):
    page.set_core(FakeCore({"demo": payload(problems=["b: needs 'ghost'"])}))
    page.set_inventory(FakeInventory([row()]))
    page.open("demo")
    assert "ghost" in page.output.problems.toPlainText()
    assert "1 problem" in page.state.text()


def test_the_file_itself_is_there_to_read(opened):
    assert "id: demo" in opened.output.yaml.toPlainText()


def test_a_core_that_cannot_be_reached_is_reported_rather_than_raising(page):
    page.set_core(FakeCore({"demo": payload()}))
    page.core.raise_on_show = "could not run the launcher"
    page.set_inventory(FakeInventory([row()]))
    page.open("demo")

    assert page.canvas.node_ids() == []
    assert "could not read" in page.state.text()
    assert "could not run" in page.output.problems.toPlainText()


def test_opening_nothing_does_nothing(page):
    page.set_core(FakeCore())
    page.open("")
    assert page.canvas.node_ids() == []


def test_opening_before_a_core_is_configured_does_not_raise(page):
    page.open("demo")


def test_what_was_open_survives_the_inventory_being_read_again(opened):
    """Saving a scenario elsewhere must not close the cycle somebody is reading."""
    opened.set_inventory(FakeInventory([row()], [SHELL_PLUGIN]))
    assert sorted(opened.canvas.node_ids()) == ["a", "b", "c", "d"]


# ------------------------------------------------------------------ inspector
# ----------------------------------------------------------------- live runs
def test_a_run_repaints_the_nodes_it_already_drew(opened):
    """The scene is never rebuilt during a run: forty nodes flickering on every
    event is the difference between a live view and an unusable one."""
    state = RunState()
    opened.set_run_state(state)
    before = id(opened.canvas.node("a"))

    state.handle({"kind": "cycle.run.start", "run_id": "r", "cycle": "demo",
                  "graph": DIAMOND})
    state.handle({"kind": "cycle.step.end", "run_id": "r", "step": "a",
                  "status": "success", "duration_ms": 1200})

    assert opened.canvas.node("a").status == "success"
    assert id(opened.canvas.node("a")) == before


def test_reading_the_cycle_again_keeps_what_the_run_has_done(opened):
    """The complaint this answers: a run still waiting, somebody goes to look
    at something else, comes back, and every node has gone blank."""
    state = RunState()
    opened.set_run_state(state)
    state.handle({"kind": "cycle.run.start", "run_id": "r", "cycle": "demo",
                  "graph": DIAMOND})
    state.handle({"kind": "cycle.step.end", "run_id": "r", "step": "a",
                  "status": "success", "duration_ms": 1200})
    state.handle({"kind": "cycle.step.waiting", "run_id": "r", "step": "b",
                  "message": "nothing ready yet"})
    before = id(opened.canvas.node("a"))

    opened.open("demo")                    # a Refresh, a click in the explorer

    assert opened.canvas.node("a").status == "success"
    assert opened.canvas.node("b").status == "waiting"
    # And the scene itself was not rebuilt to do it, so the selection and the
    # view somebody had are still theirs.
    assert id(opened.canvas.node("a")) == before


def test_a_rebuilt_canvas_is_painted_from_what_the_run_knows(opened):
    """The paths that do have to rebuild - a cycle read fresh - must come back
    with the run on them rather than blank until the next event."""
    state = RunState()
    opened.set_run_state(state)
    state.handle({"kind": "cycle.run.start", "run_id": "r", "cycle": "demo",
                  "graph": DIAMOND})
    state.handle({"kind": "cycle.step.end", "run_id": "r", "step": "a",
                  "status": "success", "duration_ms": 1200})

    _reopen(opened)

    assert opened.canvas.node("a").status == "success"


def test_another_cycle_is_not_painted_with_this_run_s_statuses(page):
    """Two cycles share step ids all the time - "todo" is in half of them."""
    opened = _two_cycles(page)
    state = RunState()
    opened.set_run_state(state)
    state.handle({"kind": "cycle.run.start", "run_id": "r", "cycle": "demo",
                  "graph": DIAMOND})
    state.handle({"kind": "cycle.step.end", "run_id": "r", "step": "a",
                  "status": "success", "duration_ms": 1200})

    opened.open("other")

    assert all(opened.canvas.node(step_id).status == "pending"
               for step_id in opened.canvas.node_ids())


def test_a_cycle_that_has_actually_changed_is_drawn_again(opened):
    """Keeping the scene is for the payload that would draw the same picture;
    a step added since has to appear."""
    grown = graph(("a", []), ("b", ["a"]), ("c", ["a"]), ("d", ["b", "c"]),
                  ("e", ["d"]))
    opened.core.cycles["demo"] = payload(cycle=grown)

    opened.open("demo")

    assert "e" in opened.canvas.node_ids()


def test_a_step_edited_without_moving_anything_is_still_taken(opened):
    """A condition added or a step disabled changes what a node says without
    changing where anything sits, and the node has to say the new thing."""
    changed = graph(("a", []), ("b", ["a"]), ("c", ["a"]), ("d", ["b", "c"]))
    for node in changed["nodes"]:
        if node["id"] == "b":
            node["condition"] = "${steps.a.outputs.passed} == 'true'"
    opened.core.cycles["demo"] = payload(cycle=changed)

    opened.open("demo")

    assert opened.canvas.node("b").node.get("condition")


def test_a_step_that_is_still_going_says_so(opened):
    state = RunState()
    opened.set_run_state(state)
    state.handle({"kind": "cycle.run.start", "run_id": "r", "graph": DIAMOND})
    state.handle({"kind": "cycle.step.start", "run_id": "r", "step": "b",
                  "plugin": "command.shell", "attempt": 1})
    assert opened.canvas.node("b").status == "running"


def test_a_skipped_step_is_painted_as_skipped(opened):
    state = RunState()
    opened.set_run_state(state)
    state.handle({"kind": "cycle.run.start", "run_id": "r", "graph": DIAMOND})
    state.handle({"kind": "cycle.step.skipped", "run_id": "r", "step": "d",
                  "status": "skipped", "reason": "b failed"})
    assert opened.canvas.node("d").status == "skipped"


# ---------------------------------------------------------------- the output
def _running(page):
    """A page with a run in progress and output from two steps."""
    state = RunState()
    page.set_run_state(state)
    state.handle({"kind": "cycle.run.start", "run_id": "r", "graph": DIAMOND})
    state.handle({"kind": "cycle.step.log", "run_id": "r", "step": "a",
                  "stream": "out", "lines": ["a said this"]})
    state.handle({"kind": "cycle.step.log", "run_id": "r", "step": "b",
                  "stream": "out", "lines": ["b said that"]})
    settled()
    return state


def test_with_nothing_picked_the_output_is_the_whole_run(opened):
    """Having to click a node to see anything meant a running cycle showed
    nothing until you guessed which node was talking."""
    _running(opened)
    text = opened.output.log.toPlainText()
    assert "a said this" in text and "b said that" in text
    assert opened.output.tabs.tabText(TAB_OUTPUT) == "Output"


def test_the_run_wide_output_says_which_step_each_line_came_from(opened):
    _running(opened)
    assert "a | a said this" in opened.output.log.toPlainText()


def test_picking_a_node_narrows_the_output_to_it(opened):
    _running(opened)
    opened._node_selected("a")
    text = opened.output.log.toPlainText()
    assert "a said this" in text
    assert "b said that" not in text
    assert opened.output.tabs.tabText(TAB_OUTPUT).endswith("a")


def test_unpicking_widens_it_again(opened):
    _running(opened)
    opened._node_selected("a")
    opened._node_selected("")
    assert "b said that" in opened.output.log.toPlainText()


def test_a_new_line_refreshes_the_wide_view_without_a_node_being_picked(opened):
    state = _running(opened)
    state.handle({"kind": "cycle.step.log", "run_id": "r", "step": "d",
                  "stream": "out", "lines": ["late arrival"]})
    settled()
    assert "late arrival" in opened.output.log.toPlainText()


def test_the_output_follows_the_newest_line(opened, qapp):
    opened.output.resize(700, 200)
    opened.output.show()
    opened.output.set_log([("out", "line %d" % n) for n in range(80)])
    qapp.processEvents()

    bar = opened.output.log.verticalScrollBar()
    assert bar.maximum() > 0
    assert bar.value() == bar.maximum()


def test_a_reader_who_scrolled_up_is_not_thrown_back_down(opened, qapp):
    """During a run the output is replaced several times a second. Being sent
    to the bottom each time makes a step's failure impossible to read."""
    opened.output.resize(700, 200)
    opened.output.show()
    opened.output.set_log([("out", "line %d" % n) for n in range(80)])
    qapp.processEvents()
    bar = opened.output.log.verticalScrollBar()
    bar.setValue(10)

    opened.output.set_log([("out", "line %d" % n) for n in range(140)])
    qapp.processEvents()
    assert bar.value() == 10


# ---------------------------------------------------------------- the stages
def test_a_step_s_stages_are_shown_as_rows(opened):
    state = RunState()
    opened.set_run_state(state)
    state.handle({"kind": "cycle.run.start", "run_id": "r", "graph": DIAMOND})
    state.handle({"kind": "cycle.step.stage", "run_id": "r", "step": "a",
                  "phase": "thinking", "title": "Thinking"})
    state.handle({"kind": "cycle.step.stage", "run_id": "r", "step": "a",
                  "phase": "tool", "title": "Read main.py"})
    settled()

    assert opened.output.stages.titles() == ["Thinking", "Read main.py"]
    assert opened.output.tabs.tabText(TAB_STAGES) == "Stages (2)"


def test_picking_a_node_leaves_the_stages_showing_the_whole_run(opened):
    """Narrowing them with the output emptied the panel far more often than it
    made it precise: only an agent step reports stages, so clicking any other
    node blanked the only readable account of what the cycle was doing."""
    state = RunState()
    opened.set_run_state(state)
    state.handle({"kind": "cycle.run.start", "run_id": "r", "graph": DIAMOND})
    state.handle({"kind": "cycle.step.stage", "run_id": "r", "step": "a",
                  "title": "from a"})
    state.handle({"kind": "cycle.step.stage", "run_id": "r", "step": "b",
                  "title": "from b"})

    opened._node_selected("b")
    assert opened.output.stages.titles() == ["from a", "from b"]
    # The output, though, is that step's alone - that is what picking is for.
    assert opened.output.tabs.tabText(TAB_OUTPUT).endswith("b")


def test_a_stage_of_an_unpicked_step_still_reaches_the_panel(opened):
    """With a node picked, stages from every other step stopped arriving: the
    repaint was asked for only when the step that moved was the selected one."""
    state = RunState()
    opened.set_run_state(state)
    state.handle({"kind": "cycle.run.start", "run_id": "r", "graph": DIAMOND})
    opened._node_selected("b")
    state.handle({"kind": "cycle.step.stage", "run_id": "r", "step": "a",
                  "title": "from a"})
    settled()
    assert opened.output.stages.titles() == ["from a"]


def test_a_cycle_whose_steps_report_no_stages_leaves_the_tab_plain(opened):
    _running(opened)
    assert opened.output.stages.titles() == []
    assert opened.output.tabs.tabText(TAB_STAGES) == "Stages"


def test_starting_a_run_clears_what_the_last_one_left(opened):
    state = RunState()
    opened.set_run_state(state)
    state.handle({"kind": "cycle.run.start", "run_id": "r", "graph": DIAMOND})
    state.handle({"kind": "cycle.step.stage", "run_id": "r", "step": "a",
                  "title": "old news"})
    opened._run()
    assert opened.output.stages.titles() == []
    assert opened.output.log.toPlainText() == ""


def test_an_event_about_a_step_that_is_not_here_is_ignored(opened):
    """A run may be of a cycle that has since been edited."""
    state = RunState()
    opened.set_run_state(state)
    state.handle({"kind": "cycle.run.start", "run_id": "r", "graph": DIAMOND})
    state.handle({"kind": "cycle.step.end", "run_id": "r", "step": "ghost",
                  "status": "success"})


def test_a_run_started_elsewhere_draws_itself(page):
    """From History, or a rerun: the canvas shows what is going, not what was
    last clicked."""
    page.set_core(FakeCore({"demo": payload()}))
    page.set_inventory(FakeInventory([row()], [SHELL_PLUGIN]))
    state = RunState()
    page.set_run_state(state)

    state.handle({"kind": "cycle.run.start", "run_id": "r", "graph": DIAMOND})
    assert sorted(page.canvas.node_ids()) == ["a", "b", "c", "d"]


def test_a_step_s_output_is_shown_when_it_is_the_selected_one(opened):
    state = RunState()
    opened.set_run_state(state)
    state.handle({"kind": "cycle.run.start", "run_id": "r", "graph": DIAMOND})
    opened.canvas.select("a")
    state.handle({"kind": "cycle.step.log", "run_id": "r", "step": "a",
                  "stream": "out", "lines": ["building", "done"]})
    settled()

    assert "building" in opened.output.log.toPlainText()


def test_another_step_s_output_does_not_appear_under_the_selected_one(opened):
    state = RunState()
    opened.set_run_state(state)
    state.handle({"kind": "cycle.run.start", "run_id": "r", "graph": DIAMOND})
    opened.canvas.select("a")
    state.handle({"kind": "cycle.step.log", "run_id": "r", "step": "b",
                  "stream": "out", "lines": ["not mine"]})

    assert "not mine" not in opened.output.log.toPlainText()


# -------------------------------------------------------------------- running
def test_running_asks_for_the_open_cycle_by_id(opened):
    asked = []
    opened.run_requested.connect(lambda *one: asked.append(one))
    opened._run()
    assert asked == [("demo", "", "")], "all of it, and no step named"


def test_full_run_button_sends_a_text_mode_without_conversion_warnings(opened, capfd):
    requested = []
    opened.run_requested.connect(lambda *args: requested.append(args))
    opened.canvas.select("b")
    capfd.readouterr()

    opened.run_button.click()

    captured = capfd.readouterr()
    assert "Cannot copy-convert" not in captured.err
    assert requested == [("demo", "", "")]


def test_resume_uses_the_displayed_run_without_requiring_a_selected_node(opened):
    state = RunState()
    opened.set_run_state(state)
    state.handle({"kind": "cycle.run.start", "run_id": "saved-run", "cycle": "demo",
                  "graph": DIAMOND})
    state.handle({"kind": "cycle.step.end", "run_id": "saved-run", "step": "a",
                  "status": "success", "outputs": {"plan": "saved"}})
    state.handle({"kind": "cycle.run.end", "run_id": "saved-run", "cycle": "demo",
                  "status": "failed", "exit_code": 1})
    settled()
    requests = []
    opened.run_requested.connect(lambda *args: requests.append(args))
    assert opened.run_button.isEnabled()
    assert opened.run_button.text() == "Resume"
    opened.run_button.click()
    assert requests == [("demo", "resume", "saved-run")]
    assert opened.canvas.node("a").status == "success"


def test_resume_is_unavailable_for_a_running_or_successful_cycle(opened):
    state = RunState()
    opened.set_run_state(state)
    state.handle({"kind": "cycle.run.start", "run_id": "r", "cycle": "demo",
                  "graph": DIAMOND})
    settled()
    assert not opened.run_button.isEnabled()
    assert not opened.hard_run_action.isEnabled()
    state.handle({"kind": "cycle.run.end", "run_id": "r", "cycle": "demo",
                  "status": "success", "exit_code": 0})
    settled()
    assert opened.run_button.isEnabled()
    assert opened.run_button.text() == "Run"
    assert opened.hard_run_action.isEnabled()


@pytest.mark.parametrize("status", ["cancelled", "interrupted", "timeout", "error"])
def test_primary_action_resumes_a_stopped_run(opened, status):
    state = RunState()
    opened.set_run_state(state)
    state.handle({"kind": "cycle.run.start", "run_id": "saved", "cycle": "demo",
                  "graph": DIAMOND})
    state.handle({"kind": "cycle.run.end", "run_id": "saved", "cycle": "demo",
                  "status": status, "exit_code": 1})
    settled()
    requests = []
    opened.run_requested.connect(lambda *args: requests.append(args))
    opened.run_button.click()
    assert opened.run_button.text() == "Resume"
    assert requests == [("demo", "resume", "saved")]


def test_hard_run_menu_explicitly_starts_fresh_even_with_a_failed_run(opened):
    from PySide6.QtWidgets import QToolButton

    state = RunState()
    opened.set_run_state(state)
    state.handle({"kind": "cycle.run.start", "run_id": "saved", "cycle": "demo",
                  "graph": DIAMOND})
    state.handle({"kind": "cycle.run.end", "run_id": "saved", "cycle": "demo",
                  "status": "failed", "exit_code": 1})
    settled()
    opened.canvas.select("b")
    requests = []
    opened.run_requested.connect(lambda *args: requests.append(args))
    assert opened.run_button.popupMode() == QToolButton.MenuButtonPopup
    assert opened.run_button.property("hasmenu") == "true"
    assert [action.text() for action in opened.run_button.menu().actions()] == ["Hard Run"]
    opened.hard_run_action.trigger()
    assert requests == [("demo", "", "")]


def test_another_cycles_failed_run_does_not_change_primary_action(opened):
    state = RunState()
    opened.set_run_state(state)
    state.handle({"kind": "cycle.run.start", "run_id": "saved", "cycle": "other",
                  "graph": DIAMOND})
    state.handle({"kind": "cycle.run.end", "run_id": "saved", "cycle": "other",
                  "status": "failed", "exit_code": 1})
    settled()
    assert opened.run_button.text() == "Run"


def test_running_clears_the_marks_from_the_last_time(opened):
    opened.canvas.set_status("a", "failed")
    opened._run()
    assert opened.canvas.node("a").status == "pending"


def test_a_cycle_with_problems_is_not_run(page, monkeypatch):
    """It would fail on the first step, minutes into somebody's afternoon."""
    page.set_core(FakeCore({"demo": payload(problems=["b: needs 'ghost'"])}))
    page.set_inventory(FakeInventory([row()]))
    page.open("demo")

    said = []
    monkeypatch.setattr("cms_gui.widgets.note",
                        lambda *args, **kwargs: said.append(args[-1]))

    asked = []
    page.run_requested.connect(asked.append)
    page._run()

    assert asked == []
    assert said and "ghost" in said[0]


def test_run_is_offered_only_once_a_cycle_is_open(page):
    page.set_core(FakeCore({"demo": payload()}))
    page.set_inventory(FakeInventory([row()]))
    assert not page.run_button.isEnabled()
    page.open("demo")
    assert page.run_button.isEnabled()


def test_stop_is_offered_only_while_a_run_is_going(opened):
    state = RunState()
    opened.set_run_state(state)
    assert not opened.stop_button.isEnabled()

    state.handle({"kind": "cycle.run.start", "run_id": "r", "graph": DIAMOND})
    assert opened.stop_button.isEnabled()
    assert not opened.run_button.isEnabled()

    state.handle({"kind": "cycle.run.end", "run_id": "r", "status": "success",
                  "exit_code": 0})
    settled()
    assert not opened.stop_button.isEnabled()
    assert opened.run_button.isEnabled()


def test_a_scenario_run_does_not_make_the_cycle_look_live(opened):
    """The two used to share one flag, so starting anything on the Run page lit
    Stop here for a cycle that had finished long before."""
    state = RunState()
    opened.set_run_state(state)
    state.handle({"kind": "cycle.run.start", "run_id": "r", "graph": DIAMOND})
    state.handle({"kind": "cycle.step.stage", "run_id": "r", "step": "a",
                  "title": "thought about it"})
    state.handle({"kind": "cycle.run.end", "run_id": "r", "status": "success",
                  "exit_code": 0})

    state.reset()                            # what start_run does
    state.handle({"kind": "launcher.start", "users": []})
    settled()
    assert not opened.stop_button.isEnabled()
    # And the cycle it was showing is still there to read.
    assert opened.output.stages.titles() == ["thought about it"]


def test_stopping_says_so(opened):
    asked = []
    opened.stop_requested.connect(lambda: asked.append(True))
    opened.stop_button.setEnabled(True)
    opened.stop_button.click()
    assert asked == [True]


# ------------------------------------------------------------------- the view
def test_the_canvas_can_be_zoomed_and_fitted(opened):
    opened.resize(900, 600)
    before = opened.canvas.transform().m11()
    opened.canvas.zoom_in()
    assert opened.canvas.transform().m11() > before
    opened.canvas.zoom_out()
    opened.canvas.fit()


def test_zooming_stops_at_the_ends(opened):
    opened.resize(900, 600)
    floor = opened.canvas._floor

    for _ in range(40):
        opened.canvas.zoom_in()
    assert opened.canvas.transform().m11() <= opened.canvas._spec["zoom"]["max"]

    for _ in range(80):
        opened.canvas.zoom_out()
    assert opened.canvas.transform().m11() >= floor


def test_a_graph_fitted_below_the_usual_minimum_can_still_be_zoomed_in(opened):
    """A fixed minimum would have made Fit a one-way door on a large cycle."""
    opened.canvas.scale(0.01 / (opened.canvas.transform().m11() or 1.0),
                        0.01 / (opened.canvas.transform().m11() or 1.0))
    opened.canvas._floor = 0.01
    before = opened.canvas.transform().m11()
    opened.canvas.zoom_in()
    assert opened.canvas.transform().m11() > before


def test_a_canvas_that_has_never_been_shown_is_not_left_at_zero(opened):
    """fitInView against a viewport with no area gives a degenerate transform,
    and everything drawn afterwards would be invisible at any zoom."""
    opened.canvas.fit()
    assert opened.canvas.transform().m11() > 0


def test_the_canvas_survives_the_palette_moving_underneath_it(opened):
    """theme.set_dark_mode rewrites its module globals in place."""
    opened.canvas.restyle()


# ------------------------------------------------------- moving things around
# A computed layout is a starting point, not an answer: on a real cycle there is
# always one node somebody wants out of the way to read the rest. Dragging is
# safe to offer because Arrange undoes all of it.

def test_a_node_can_be_dragged(opened):
    node = opened.canvas.node("a")
    assert node.flags() & node.GraphicsItemFlag.ItemIsMovable


def test_a_dragged_node_takes_its_connections_with_it(opened):
    """Otherwise the graph stops being a picture of anything after one drag."""
    edge = opened.canvas._edges[0]
    before = edge.path().elementAt(0).y
    source = opened.canvas.node(edge.source)
    source.setPos(source.pos().x(), source.pos().y() + 240)
    assert edge.path().elementAt(0).y != before


def test_arrange_puts_everything_back(opened):
    node = opened.canvas.node("a")
    home = node.pos()
    node.setPos(home.x() + 500, home.y() + 500)

    opened.canvas.arrange()
    assert node.pos() == home


def test_arrange_is_offered_only_once_something_has_been_moved(opened):
    assert not opened.arrange_button.isEnabled()
    opened.canvas._moved = True
    opened._update_buttons()
    assert opened.arrange_button.isEnabled()


def test_a_node_dragged_off_the_edge_is_still_somewhere_the_view_can_reach(opened):
    """The view can only scroll to what the scene says exists."""
    node = opened.canvas.node("a")
    node.setPos(3000, 2000)
    opened.canvas._resize_scene()
    assert opened.canvas.scene().sceneRect().contains(3000, 2000)


def _two_cycles(page):
    """A page that can open two different cycles, for the tests that need one
    open to be genuinely different from the other."""
    other = graph(("x", []), ("y", ["x"]))
    other["id"] = "other"
    page.set_core(FakeCore({"demo": payload(),
                            "other": payload("other", cycle=other)}))
    page.set_inventory(FakeInventory([row(), row("other", "Other cycle")],
                                     [SHELL_PLUGIN]))
    page.open("demo")
    return page


def test_opening_another_cycle_forgets_that_anything_was_moved(page):
    opened = _two_cycles(page)
    opened.canvas._moved = True
    opened.open("other")
    assert not opened.canvas.moved()


# ------------------------------------------------------------- moving the view
def test_the_view_keeps_itself_fitted_until_somebody_takes_over(opened):
    """A splitter pane changes size whenever anything else on the page does."""
    assert not opened.canvas._touched
    opened.canvas.zoom_in()
    assert opened.canvas._touched


def test_fit_hands_control_back(opened):
    opened.canvas.zoom_in()
    opened.canvas.fit()
    assert not opened.canvas._touched


def _resize(canvas, monkeypatch, width=700, height=500):
    """Deliver a resize and report how it asked to be placed again, if at all.

    The handler is called directly with a real event: Qt does not deliver a
    resize to a hidden widget at all, so ``canvas.resize(...)`` in a headless
    test exercises nothing. The decision is what is under test rather than the
    pixels, which would say more about Qt's layout timing than about this code.
    """
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QResizeEvent

    placed = []
    monkeypatch.setattr(type(canvas), "fit",
                        lambda self, at_least=None: placed.append("fit"))
    monkeypatch.setattr(type(canvas), "reset_view",
                        lambda self: placed.append("default"))
    canvas.resizeEvent(QResizeEvent(QSize(width, height), canvas.size()))
    return placed


def test_resizing_places_the_view_again_while_nobody_has_taken_over(opened,
                                                                    monkeypatch):
    """A splitter pane changes size whenever anything else on the page does."""
    assert _resize(opened.canvas, monkeypatch) == ["default"]


def test_a_resize_after_fit_repeats_the_fit_not_the_opening_view(opened,
                                                                 monkeypatch):
    """Otherwise pressing Fit and then dragging the splitter would undo what
    was just asked for."""
    opened.canvas.fit()
    assert _resize(opened.canvas, monkeypatch) == ["fit"]


def test_resizing_leaves_a_view_somebody_zoomed_alone(opened, monkeypatch):
    """A canvas that snaps back to Fit every time the splitter moves is
    unusable."""
    opened.canvas.zoom_in()
    assert not _resize(opened.canvas, monkeypatch)


def test_a_canvas_somebody_panned_is_left_alone_on_resize_too(opened,
                                                              monkeypatch):
    opened.canvas._touched = True
    assert not _resize(opened.canvas, monkeypatch)


def test_a_canvas_with_nothing_drawn_has_nothing_to_fit(page, monkeypatch):
    page.set_core(FakeCore())
    page.set_inventory(FakeInventory([]))
    assert not _resize(page.canvas, monkeypatch)


# ------------------------------------------------------------------- the edges
def test_an_edge_knows_the_two_steps_it_joins(opened):
    joins = {(edge.source, edge.target) for edge in opened.canvas._edges}
    assert joins == {("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")}


def test_an_edge_to_a_step_that_is_not_in_the_graph_is_dropped(page):
    """Rather than drawn to nowhere, or raising in a paint path."""
    one = graph(("a", []))
    one["edges"].append({"from": "a", "to": "ghost", "kind": "dependency"})
    page.set_core(FakeCore({"demo": payload(cycle=one)}))
    page.set_inventory(FakeInventory([row()], [SHELL_PLUGIN]))
    page.open("demo")
    assert page.canvas._edges == []


def _clearance(node, point):
    """How far a point sits outside a node's box, on whichever side is nearest.

    Measured against the box rather than along one axis, so these say what they
    mean whichever way the graph is laid out - the direction of the flow is a
    value in SPEC, and a test written in x would quietly stop checking anything
    the day it changed.
    """
    from cms_gui.cyclegraph import SPEC

    left, top = node.pos().x(), node.pos().y()
    right = left + SPEC["node"]["w"]
    bottom = top + SPEC["node"]["h"]
    return max(max(left - point.x, point.x - right, 0),
               max(top - point.y, point.y - bottom, 0))


def test_an_edge_stops_short_of_the_node_it_points_at(opened):
    """So the head is drawn on clear ground rather than over its own line."""
    edge = opened.canvas._edges[0]
    target = opened.canvas.node(edge.target)
    last = edge.path().elementAt(edge.path().elementCount() - 1)
    assert _clearance(target, last) > 0


def test_an_edge_reaches_far_enough_to_be_read_as_touching_the_node(opened):
    edge = opened.canvas._edges[0]
    target = opened.canvas.node(edge.target)
    last = edge.path().elementAt(edge.path().elementCount() - 1)
    assert _clearance(target, last) < 20


# ------------------------------------------------------------------ the surface
def test_the_canvas_can_be_dragged_even_when_the_whole_cycle_fits(opened):
    """A scene sized exactly to its contents has nowhere to scroll, so panning
    would quietly stop working on precisely the small cycles where moving things
    about is easiest."""
    opened.canvas.resize(1600, 1000)        # far larger than the graph
    opened.canvas._resize_scene()

    scene = opened.canvas.scene().sceneRect()
    content = opened.canvas.content_rect()
    assert scene.width() > content.width() + 400
    assert scene.height() > content.height() + 400
    assert scene.contains(content)


def test_fit_frames_the_graph_rather_than_the_room_around_it(opened):
    """Fitting the scene would leave the cycle as a postage stamp."""
    opened.canvas.resize(1600, 1000)
    opened.canvas._resize_scene()
    opened.canvas.fit()

    shown = opened.canvas.mapToScene(
        opened.canvas.viewport().rect()).boundingRect()
    content = opened.canvas.content_rect()
    assert shown.width() < opened.canvas.scene().sceneRect().width()
    assert shown.width() >= content.width() - 1


def test_a_canvas_with_nothing_on_it_has_no_room_to_roam(page):
    page.set_core(FakeCore())
    page.set_inventory(FakeInventory([]))
    page.canvas._resize_scene()
    assert page.canvas.scene().sceneRect().width() <= 1


def test_the_canvas_paints_its_own_ground_rather_than_the_page_s(opened):
    """It is a place you look into, not more page - and the grid is the only
    thing that shows the surface moving when empty ground is dragged."""
    from cms_gui import theme

    assert opened.canvas.backgroundBrush().color().name().lower() == (
        theme.CANVAS_BG.lower())


def test_the_ground_follows_the_palette_into_dark_mode(opened):
    from cms_gui import theme

    theme.set_dark_mode(True)
    try:
        opened.canvas.restyle()
        assert opened.canvas.backgroundBrush().color().name().lower() == (
            theme.CANVAS_BG.lower())
    finally:
        theme.set_dark_mode(False)
        opened.canvas.restyle()


# --------------------------------------------------------------- the projects
# The projects file is the page's alone. Which cycles belong to one is not in
# it: each cycle says so itself, so a cycle file copied to another machine still
# knows where it belongs.

@pytest.fixture
def owning(page, tmp_path, monkeypatch):
    """A page with a real projects file it may write to."""
    page._projects_path = str(tmp_path / "cycleprojects.json")
    page.set_core(FakeCore({"demo": payload(project="Portal")}))
    page._projects = projects("Portal")
    page.set_inventory(FakeInventory([row(project="Portal")], [SHELL_PLUGIN]))
    monkeypatch.setattr("cms_gui.widgets.warn",
                        lambda *a, **k: None)
    monkeypatch.setattr("cms_gui.widgets.note",
                        lambda *a, **k: None)
    return page


def _answer(monkeypatch, text):
    monkeypatch.setattr("cms_gui.pages.cycles.QInputDialog.getText",
                        lambda *a, **k: (text, True))


def _cancel(monkeypatch):
    monkeypatch.setattr("cms_gui.pages.cycles.QInputDialog.getText",
                        lambda *a, **k: ("", False))


def _agree(monkeypatch, yes=True):
    monkeypatch.setattr("cms_gui.widgets.confirm", lambda *a, **k: yes)


def test_a_new_project_is_written_to_the_file(owning, monkeypatch):
    from cms_gui import cycleprojectsfile as cpf

    _answer(monkeypatch, "Release")
    owning._new_project()

    assert cpf.names(cpf.load(owning._projects_path)) == ["Portal", "Release"]


def test_a_new_project_is_dated_so_the_page_can_order_them(owning, monkeypatch):
    from cms_gui import cycleprojectsfile as cpf

    _answer(monkeypatch, "Release")
    owning._new_project()
    assert cpf.find(owning._projects, "Release").added


def test_cancelling_writes_nothing(owning, monkeypatch):
    import os

    _cancel(monkeypatch)
    owning._new_project()
    assert not os.path.exists(owning._projects_path)


def test_a_name_already_taken_is_refused(owning, monkeypatch):
    _answer(monkeypatch, "Portal")
    owning._new_project()
    assert len(owning._projects) == 1


def test_renaming_a_project_rewrites_the_cycles_that_name_it(owning, monkeypatch):
    """Otherwise a rename quietly moves every one of them to Unassigned."""
    _answer(monkeypatch, "Customer Portal")
    _agree(monkeypatch)
    owning._rename_project("Portal")

    from cms_gui import cycleprojectsfile as cpf
    assert cpf.names(owning._projects) == ["Customer Portal"]
    written = [document for _id, document in owning.core.saved]
    assert written and written[0]["project"] == "Customer Portal"


def test_renaming_can_be_called_off_before_anything_moves(owning, monkeypatch):
    _answer(monkeypatch, "Customer Portal")
    _agree(monkeypatch, yes=False)
    owning._rename_project("Portal")

    from cms_gui import cycleprojectsfile as cpf
    assert cpf.names(owning._projects) == ["Portal"]
    assert owning.core.saved == []


def test_deleting_a_project_keeps_its_cycles(owning, monkeypatch):
    """The files are kept and move to Unassigned - losing them would be theft."""
    _agree(monkeypatch)
    owning._delete_project("Portal")

    assert owning._projects == []
    assert owning.core.deleted == []


def test_deleting_can_be_called_off(owning, monkeypatch):
    _agree(monkeypatch, yes=False)
    owning._delete_project("Portal")
    assert len(owning._projects) == 1


def test_a_project_that_is_not_there_is_not_an_error(owning, monkeypatch):
    _agree(monkeypatch)
    owning._delete_project("Never existed")
    owning._rename_project("Never existed")


def test_the_projects_file_is_read_back_from_where_settings_says(page, tmp_path):
    from cms_gui import cycleprojectsfile as cpf

    path = str(tmp_path / "elsewhere.json")
    cpf.save(path, [cpf.ProjectRow(name="From the file")])
    page.set_projects_path(path)

    assert cpf.names(page._projects) == ["From the file"]


def test_a_missing_projects_file_is_simply_no_projects_yet(page, tmp_path):
    page.set_projects_path(str(tmp_path / "not-there.json"))
    assert page._projects == []


def test_a_broken_projects_file_is_reported_rather_than_ignored(page, tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    page.set_projects_path(str(path))

    assert page._projects == []
    assert "broken.json" in page.output.problems.toPlainText()


# ----------------------------------------------------------------- the cycles
def test_a_new_cycle_is_written_with_one_real_step(owning, monkeypatch):
    """The core refuses anything that does not hold, so an empty file would be
    something somebody has to fix before they can save it again."""
    _answer(monkeypatch, "nightly")
    owning._new_cycle("Portal")

    cycle_id, document = owning.core.saved[0]
    assert cycle_id == "nightly"
    assert document["project"] == "Portal"
    assert document["steps"] and document["steps"][0]["plugin"]


def test_a_new_cycle_in_no_project_carries_none(owning, monkeypatch):
    _answer(monkeypatch, "loose")
    owning._new_cycle("")
    assert "project" not in owning.core.saved[0][1]


def test_an_id_already_taken_is_refused(owning, monkeypatch):
    _answer(monkeypatch, "demo")
    owning._new_cycle("Portal")
    assert owning.core.saved == []


def test_duplicating_copies_the_whole_document_under_a_new_id(owning,
                                                              monkeypatch):
    _answer(monkeypatch, "demo_copy")
    owning._duplicate_cycle("demo")

    cycle_id, document = owning.core.saved[0]
    assert cycle_id == "demo_copy"
    assert document["id"] == "demo_copy"
    assert len(document["steps"]) == 4
    assert "(copy)" in document["name"]


def test_renaming_changes_the_display_name_and_keeps_the_id(owning,
                                                           monkeypatch):
    _answer(monkeypatch, "Nightly validation")
    owning._rename_cycle("demo")

    cycle_id, document = owning.core.saved[0]
    assert cycle_id == "demo"
    assert document["id"] == "demo"
    assert document["name"] == "Nightly validation"
    assert owning.core.deleted == []


def test_a_cycle_that_ships_with_the_app_cannot_be_renamed(owning, monkeypatch):
    owning.core.cycles["demo"] = payload(writable=False)
    _answer(monkeypatch, "renamed")
    owning._rename_cycle("demo")

    assert owning.core.saved == []
    assert owning.core.deleted == []


def test_deleting_a_cycle_asks_first(owning, monkeypatch):
    _agree(monkeypatch, yes=False)
    owning._delete_cycle("demo")
    assert owning.core.deleted == []

    _agree(monkeypatch)
    owning._delete_cycle("demo")
    assert owning.core.deleted == ["demo"]


def test_deleting_what_is_open_leaves_nothing_on_the_canvas(owning, monkeypatch):
    owning.open("demo")
    assert owning.canvas.node_ids()

    _agree(monkeypatch)
    owning._delete_cycle("demo")
    assert owning.canvas.node_ids() == []
    assert owning.current_id() == ""


# ------------------------------------------------------------- the properties
# The form itself is tested in test_cycledialogs.py. What belongs here is the
# half the page owns: reading which secrets are set before the form opens, and
# putting the edited ones somewhere that is not the cycle file.
def test_the_page_asks_which_secrets_are_set_never_what_they_are(owning):
    owning.core.stored["demo"] = {"token": "ghp_example"}
    assert owning._stored_secrets("demo") == ("token",)


def test_a_cycle_with_no_secrets_asks_for_nothing(owning):
    assert owning._stored_secrets("demo") == ()


def test_an_edited_secret_goes_to_the_store_and_not_to_the_cycle_file(owning):
    problems = owning._write_secrets("demo", {"token": "ghp_new"}, [])

    assert problems == []
    assert owning.core.secret_writes == [("demo", "token", "ghp_new")]
    assert owning.core.saved == []          # the value never reaches a document


def test_a_secret_that_stopped_being_one_is_taken_out_of_the_store(owning):
    owning.core.stored["demo"] = {"token": "ghp_example"}
    owning._write_secrets("demo", {}, ["token"])

    assert owning.core.secret_deletes == [("demo", "token")]
    assert owning.core.stored["demo"] == {}


def test_one_secret_that_would_not_save_does_not_stop_the_others(owning):
    def refuse(cycle_id, name, value):
        owning.core.secret_writes.append((cycle_id, name, value))
        if name == "bad":
            return {"ok": False, "problems": ["the store is read-only"]}
        return {"ok": True, "problems": []}

    owning.core.cycle_secret_set = refuse
    problems = owning._write_secrets("demo", {"bad": "1", "good": "2"}, [])

    assert [name for _id, name, _value in owning.core.secret_writes] == ["bad",
                                                                         "good"]
    assert len(problems) == 1 and "read-only" in problems[0]


def test_deleting_a_cycle_forgets_its_secrets_too(owning, monkeypatch):
    """A credential nobody can see and nobody deletes is the thing to avoid."""
    owning.core.stored["demo"] = {"token": "ghp_example"}
    _agree(monkeypatch)
    owning._delete_cycle("demo")

    assert owning.core.secret_deletes == [("demo", None)]
    assert "demo" not in owning.core.stored


# ------------------------------------------------------------------- the yaml
def test_the_file_can_be_edited_and_saved(owning):
    owning.open("demo")
    owning.output.yaml.setPlainText("id: demo\nname: Edited\nsteps: []\n")
    owning.output.save_button.click()

    _id, document = owning.core.saved[0]
    assert document["yaml"].startswith("id: demo")


def test_saving_is_offered_only_once_something_changed(owning):
    owning.open("demo")
    assert not owning.output.save_button.isEnabled()

    owning.output.yaml.setPlainText("id: demo\n")
    assert owning.output.save_button.isEnabled()
    assert owning.output.tabs.tabText(TAB_YAML).endswith("*")


def test_reverting_puts_the_file_back(owning):
    owning.open("demo")
    original = owning.output.yaml.toPlainText()
    owning.output.yaml.setPlainText("nonsense")
    owning.output.revert_button.click()

    assert owning.output.yaml.toPlainText() == original
    assert not owning.output.save_button.isEnabled()


def test_a_bundled_cycle_s_file_is_read_only(owning):
    owning.core.cycles["demo"] = payload(writable=False)
    owning.open("demo")

    assert owning.output.yaml.isReadOnly()
    owning.output.yaml.setPlainText("changed anyway")
    assert not owning.output.save_button.isEnabled()




# --------------------------------------------------- where somebody put them
# The computed layout is a starting point rather than an answer: on a real
# cycle there is always one node somebody wants out of the way to read the
# rest. Until this, that arrangement lasted exactly as long as the window did.
def _reopen(page, cycle_id="demo"):
    """Read a cycle the way a newly built page reads it: nothing drawn yet.

    A page that already has the cycle on screen deliberately keeps the scene
    (see the tests above), so a test about what the *file* remembers has to
    start from a canvas with nothing on it.
    """
    page.canvas.set_graph(None)
    page.open(cycle_id)


@pytest.fixture(autouse=True)
def layout_file(tmp_path, monkeypatch):
    """The arrangement store, redirected away from the developer's own.

    For every test, not only the ones that read it: the page writes here by
    itself now - a zoom and a pan are remembered too - and one test leaving a
    view behind is the next test opening a cycle somebody had already moved.
    """
    from cms_gui import cyclelayoutfile as clf

    where = str(tmp_path / "cyclelayout.json")
    monkeypatch.setattr(clf, "default_path", lambda: where)
    return where


def test_a_dragged_node_is_still_there_when_the_cycle_is_reopened(opened,
                                                                  layout_file):
    """The complaint this answers: a canvas you can rearrange but not keep is
    a canvas nobody bothers to rearrange."""
    node = opened.canvas.node(opened.canvas.node_ids()[0])
    node.setPos(777.0, 555.0)
    opened.canvas._moved = True
    opened.canvas.nodes_moved.emit()

    _reopen(opened)

    assert opened.canvas.node(node.step_id).pos().x() == 777.0
    assert opened.canvas.node(node.step_id).pos().y() == 555.0


def test_a_node_nobody_moved_goes_where_the_layout_says(opened, layout_file):
    """So a step added since the arranging is placed rather than stacked at
    the origin."""
    from cms_gui.cyclegraph import layout_graph

    ids = opened.canvas.node_ids()
    opened.canvas.node(ids[0]).setPos(777.0, 555.0)
    opened.canvas._moved = True
    opened.canvas.nodes_moved.emit()

    _reopen(opened)
    computed = layout_graph(opened.canvas.graph()["nodes"])

    for step_id in ids[1:]:
        assert (opened.canvas.node(step_id).pos().x(),
                opened.canvas.node(step_id).pos().y()) == computed[step_id]


def test_arrange_forgets_the_arrangement_rather_than_only_undoing_it(
        opened, layout_file):
    """Otherwise the mess comes back on the next open, which would make
    Arrange look broken."""
    from cms_gui import cyclelayoutfile as clf
    from cms_gui.cyclegraph import layout_graph

    node = opened.canvas.node(opened.canvas.node_ids()[0])
    node.setPos(777.0, 555.0)
    opened.canvas._moved = True
    opened.canvas.nodes_moved.emit()
    assert clf.places_for("demo", layout_file)

    opened.canvas.arrange()

    assert clf.places_for("demo", layout_file) == {}
    _reopen(opened)
    computed = layout_graph(opened.canvas.graph()["nodes"])
    assert (opened.canvas.node(node.step_id).pos().x(),
            opened.canvas.node(node.step_id).pos().y()) == computed[node.step_id]


def test_arrange_offers_itself_on_a_cycle_opened_already_arranged(opened,
                                                                  layout_file):
    """It is the only way back to the computed layout, so it has to be
    available the moment a kept arrangement is on screen."""
    opened.canvas.node(opened.canvas.node_ids()[0]).setPos(777.0, 555.0)
    opened.canvas._moved = True
    opened.canvas.nodes_moved.emit()

    _reopen(opened)

    assert opened.canvas.moved() is True


def test_an_unwritable_store_costs_the_arrangement_and_nothing_else(opened,
                                                                    tmp_path,
                                                                    monkeypatch):
    """The least important thing on the page must not be able to break it."""
    from cms_gui import cyclelayoutfile as clf

    blocked = tmp_path / "in-the-way"
    blocked.write_text("not a directory")
    monkeypatch.setattr(clf, "default_path",
                        lambda: str(blocked / "cyclelayout.json"))

    opened.canvas.node(opened.canvas.node_ids()[0]).setPos(777.0, 555.0)
    opened.canvas._moved = True
    opened.canvas.nodes_moved.emit()       # must not raise

    _reopen(opened)                        # and the cycle still opens
    assert opened.canvas.node_ids()


# ------------------------------------------------- and where they were looking
# A zoom and a pan are the same kind of work as a dragged node, and they used to
# last exactly as long as the scene did.
def test_where_somebody_was_looking_is_kept_for_the_next_time(opened,
                                                              layout_file):
    from cms_gui import cyclelayoutfile as clf

    opened.canvas.zoom_in()
    opened._remember_view()                # what the timer does when it fires

    assert clf.view_for("demo", layout_file) == opened.canvas.view()


def test_a_remembered_view_is_what_the_cycle_opens_at(opened, layout_file):
    from cms_gui import cyclelayoutfile as clf

    clf.remember_view("demo", {"zoom": 1.25, "center": [40.0, 90.0]},
                      layout_file)

    _reopen(opened)

    assert opened.canvas.transform().m11() == pytest.approx(1.25)


def test_a_wheel_spun_through_ten_steps_writes_the_file_once(opened,
                                                             layout_file,
                                                             monkeypatch):
    """The whole point of waiting: a file rewritten per pixel to record where
    somebody is still moving to is a cost paid for nothing."""
    writes = []
    monkeypatch.setattr(opened, "_remember_view",
                        lambda: writes.append(opened.canvas.view()))

    for _ in range(10):
        opened.canvas.zoom_in()
        opened.canvas.zoom_out()

    assert writes == []
    assert opened._view_timer.isActive()


def test_leaving_the_page_settles_a_view_that_was_still_waiting(opened,
                                                                layout_file,
                                                                qapp):
    """Zooming and going straight to another section must not lose it."""
    from cms_gui import cyclelayoutfile as clf

    opened.show()
    qapp.processEvents()
    opened.canvas.zoom_in()
    assert opened._view_timer.isActive()

    opened.hide()
    qapp.processEvents()

    assert clf.view_for("demo", layout_file) is not None


def test_panning_by_the_scroll_bar_counts_as_taking_the_view_over(opened):
    """Not every way of moving a canvas goes through its own mouse events: the
    scroll bar is a widget of its own, and the keyboard is nobody's mouse."""
    bar = opened.canvas.verticalScrollBar()
    bar.setValue(bar.value() + 60)

    assert opened.canvas._touched
    assert opened._view_timer.isActive()


def test_fit_forgets_where_somebody_had_been_looking(opened, layout_file):
    """Fit is the way back, so a view surviving it would mean the next open
    undid what was just asked for."""
    from cms_gui import cyclelayoutfile as clf

    opened.canvas.zoom_in()
    opened._remember_view()
    assert clf.view_for("demo", layout_file)

    opened.fit_button.click()
    opened._flush_view()

    assert clf.view_for("demo", layout_file) is None


def test_fitting_by_double_click_forgets_it_too(opened, layout_file):
    """Fit is one gesture with three ways in - the button, a double-click on
    empty ground, Arrange - and all three mean the same thing."""
    from cms_gui import cyclelayoutfile as clf

    opened.canvas.zoom_in()
    opened._remember_view()

    opened.canvas.fit()                    # what the double-click calls
    opened._flush_view()

    assert clf.view_for("demo", layout_file) is None


def test_arrange_forgets_the_view_with_the_arrangement(opened, layout_file):
    from cms_gui import cyclelayoutfile as clf

    opened.canvas.node(opened.canvas.node_ids()[0]).setPos(777.0, 555.0)
    opened.canvas._moved = True
    opened.canvas.nodes_moved.emit()
    opened.canvas.zoom_in()
    opened._remember_view()

    opened.canvas.arrange()

    assert clf.view_for("demo", layout_file) is None
    assert clf.places_for("demo", layout_file) == {}


def test_placing_a_graph_is_not_somebody_looking_somewhere(opened, layout_file,
                                                           qapp):
    """Opening a cycle scrolls the canvas, and Qt finishes some of that a turn
    of the event loop later - taken as a gesture, every open would write down
    the view it had just restored."""
    from cms_gui import cyclelayoutfile as clf

    _reopen(opened)
    qapp.processEvents()

    assert not opened._view_timer.isActive()
    assert clf.view_for("demo", layout_file) is None


def _long_cycle(page, steps=20):
    long = graph(*[(str(n), [] if n == 0 else [str(n - 1)])
                   for n in range(steps)])
    page.set_core(FakeCore({"demo": payload(cycle=long)}))
    page.set_inventory(FakeInventory([row()], [SHELL_PLUGIN]))
    page.resize(900, 700)
    page.open("demo")
    return page


def test_a_cycle_opens_at_the_zoom_the_paper_appears_at(page):
    """Fitting twenty steps into a window answers "how big is it" and no other
    question - and answers it differently in a tall window and a short one.
    One scale for every cycle, the one the ruled paper starts at."""
    from cms_gui.cyclegraph import SPEC

    _long_cycle(page)

    assert page.canvas.transform().m11() == pytest.approx(SPEC["zoom"]["grid"])


def test_a_short_cycle_opens_at_that_zoom_too_neither_further_nor_nearer(page):
    """The same cycle should not look different for being a small one."""
    from cms_gui.cyclegraph import SPEC

    _long_cycle(page, steps=3)

    assert page.canvas.transform().m11() == pytest.approx(SPEC["zoom"]["grid"])


def _ruled(canvas, zoom):
    """Whether the ruled paper is drawn at ``zoom``. Painted, then read back."""
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QColor, QImage, QPainter

    from cms_gui import theme

    canvas.resetTransform()
    canvas.scale(zoom, zoom)
    image = QImage(60, 60, QImage.Format_RGB32)
    painter = QPainter(image)
    canvas.drawBackground(painter, QRectF(0, 0, 60 / zoom, 60 / zoom))
    painter.end()
    ground = QColor(theme.CANVAS_BG).rgb()
    return any(image.pixel(x, y) != ground
               for x in range(60) for y in range(60))


def test_the_paper_is_there_at_the_zoom_a_cycle_opens_at(page):
    """The two are one number: opening onto blank ground would be opening at a
    size the rest of the view does not agree with."""
    from cms_gui.cyclegraph import SPEC

    _long_cycle(page)

    assert _ruled(page.canvas, SPEC["zoom"]["grid"])
    assert not _ruled(page.canvas, SPEC["zoom"]["grid"] / 2)


def test_a_cycle_opens_at_its_first_node(page):
    """It runs downwards, and a list is read from its beginning."""
    _long_cycle(page)

    shown = page.canvas.mapToScene(
        page.canvas.viewport().rect()).boundingRect()
    top = page.canvas.content_rect().top()
    # Within a scroll bar's own step: it moves in whole device pixels, and one
    # of those is a couple of scene units at the zoom a cycle opens at.
    from cms_gui.cyclegraph import SPEC
    assert shown.top() == pytest.approx(top, abs=2 / SPEC["zoom"]["grid"])


def test_fit_still_shows_all_of_it_however_long(page):
    """Somebody who pressed Fit asked to see the whole thing."""
    long = graph(*[(str(n), [] if n == 0 else [str(n - 1)]) for n in range(20)])
    page.set_core(FakeCore({"demo": payload(cycle=long)}))
    page.set_inventory(FakeInventory([row()], [SHELL_PLUGIN]))
    page.resize(900, 700)
    page.open("demo")

    page.canvas.fit()
    content = page.canvas.content_rect()
    shown = page.canvas.mapToScene(page.canvas.viewport().rect()).boundingRect()
    assert shown.height() >= content.height() - 1


# ------------------------------------------------- lines that keep out of the way
def _long_chain_with_shortcuts():
    """A column with edges that skip layers - the ordinary shape of a real
    cycle, and the one that used to draw every line on top of every other."""
    steps = [("s0", [])]
    for n in range(1, 10):
        steps.append(("s%d" % n, ["s%d" % (n - 1)]))
    steps.append(("far", ["s0", "s3", "s9"]))      # three shortcuts at once
    return graph(*steps)


def _rects(canvas):
    from PySide6.QtCore import QRectF
    from cms_gui.cyclegraph import SPEC

    return {step_id: QRectF(item.pos().x(), item.pos().y(),
                            SPEC["node"]["w"], SPEC["node"]["h"])
            for step_id, item in canvas._nodes.items()}


def _through_nodes(canvas):
    """Edges whose line passes through a node it does not belong to."""
    from cms_gui.cyclegraph import _crosses, _points_of

    rects = _rects(canvas)
    return [(edge.source, edge.target) for edge in canvas._edges
            if _crosses(_points_of(edge.path()),
                        [rect for step_id, rect in rects.items()
                         if step_id not in (edge.source, edge.target)])]


def _piled(canvas):
    """How many long straight runs share a corridor with another one."""
    import collections

    from cms_gui.cyclegraph import _points_of

    runs = collections.Counter()
    for edge in canvas._edges:
        points = _points_of(edge.path())
        for one, two in zip(points, points[1:]):
            if abs(one.x() - two.x()) < 0.5 and abs(one.y() - two.y()) > 30:
                runs[round(one.x(), 1)] += 1
    return sum(count for count in runs.values() if count > 1)


@pytest.fixture
def crowded(page):
    page.set_core(FakeCore({"demo": payload(cycle=_long_chain_with_shortcuts())}))
    page.set_inventory(FakeInventory([row()], [SHELL_PLUGIN]))
    page.open("demo")
    return page


def test_no_line_is_drawn_through_a_node_it_has_nothing_to_do_with(crowded):
    """In a column every node shares an x, so an edge that skips a layer used
    to run straight down through everything between its ends."""
    assert _through_nodes(crowded.canvas) == []


def test_no_two_lines_are_drawn_down_the_same_corridor(crowded):
    """Two edges on the same pixels are one edge as far as a reader is
    concerned - which is the complaint this answers."""
    assert _piled(crowded.canvas) == 0


def test_only_the_lines_that_need_it_are_sent_around(crowded):
    """A short link between neighbours is already the clearest thing it can
    be, and bending it would be noise."""
    sent = [edge for edge in crowded.canvas._edges if edge._lane is not None]
    assert sent, "the shortcuts have to go somewhere"
    assert len(sent) < len(crowded.canvas._edges)
    for edge in sent:
        assert edge.source in ("s0", "s3"), (edge.source, edge.target)


def test_the_lines_sort_themselves_out_again_after_a_node_is_moved(crowded):
    """Dragging one step changes what the *other* edges have to go around."""
    node = crowded.canvas.node("s5")
    node.setPos(node.pos().x() - 400, node.pos().y())

    assert _through_nodes(crowded.canvas) == []


def test_a_simple_cycle_keeps_its_plain_straight_lines(opened):
    """The diamond has nothing in anybody's way; nothing should be bent."""
    assert all(edge._lane is None for edge in opened.canvas._edges)


def test_several_edges_at_one_node_leave_from_different_points(crowded):
    """Three lines from one pixel are one line to a reader."""
    starts = {(round(edge.path().elementAt(0).x, 1),
               round(edge.path().elementAt(0).y, 1))
              for edge in crowded.canvas._edges if edge.source == "s0"}
    assert len(starts) == len([edge for edge in crowded.canvas._edges
                               if edge.source == "s0"])


# ------------------------------------------------ following one step's links
# A click on a node asks "what is this connected to". On a long cycle that is a
# real question: the links run off in both directions past a dozen other boxes,
# and following one by eye means tracing a grey line through every other one.
def _lit(canvas):
    return {(edge.source, edge.target) for edge in canvas._edges if edge._lit}


def test_selecting_a_step_draws_its_links_heavier(opened):
    opened.canvas.node("b").setSelected(True)

    assert _lit(opened.canvas) == {("a", "b"), ("b", "d")}


def test_both_directions_are_lit_not_only_the_ones_leaving(opened):
    """"What does this wait for" and "what waits for this" are one question
    when you are reading a graph."""
    opened.canvas.node("b").setSelected(True)
    lit = _lit(opened.canvas)

    assert ("a", "b") in lit, "what it waits for"
    assert ("b", "d") in lit, "and what waits for it"


def test_nothing_else_is_lit(opened):
    """The whole value is the contrast: light everything and nothing stands
    out."""
    opened.canvas.node("b").setSelected(True)

    assert ("a", "c") not in _lit(opened.canvas)
    assert ("c", "d") not in _lit(opened.canvas)


def test_letting_go_of_a_step_puts_its_links_back(opened):
    opened.canvas.node("b").setSelected(True)
    opened.canvas.node("b").setSelected(False)

    assert _lit(opened.canvas) == set()


def test_selecting_another_step_moves_the_light(opened):
    opened.canvas.node("b").setSelected(True)
    opened.canvas.scene().clearSelection()
    opened.canvas.node("c").setSelected(True)

    assert _lit(opened.canvas) == {("a", "c"), ("c", "d")}


def test_a_lit_line_is_drawn_over_the_ones_it_crosses(opened):
    """Otherwise following a link across a busy graph means losing it under
    the next one."""
    from cms_gui.cyclegraph import EDGE_LIT_Z, EDGE_Z

    opened.canvas.node("b").setSelected(True)
    for edge in opened.canvas._edges:
        assert edge.zValue() == (EDGE_LIT_Z if edge._lit else EDGE_Z)


def test_a_lit_line_still_passes_under_every_node(opened):
    """A line over a node's writing is a line in the way."""
    from cms_gui.cyclegraph import EDGE_LIT_Z

    opened.canvas.node("b").setSelected(True)
    assert EDGE_LIT_Z < min(item.zValue()
                            for item in opened.canvas._nodes.values())


def test_lighting_a_link_does_not_move_it(opened):
    """It is a repaint, not a re-route: a line that jumped when you clicked
    the node it belongs to would be worse than no highlight at all."""
    edge = [one for one in opened.canvas._edges
            if (one.source, one.target) == ("a", "b")][0]
    before = edge.path()

    opened.canvas.node("b").setSelected(True)

    assert edge.path() == before


def test_opening_another_cycle_starts_with_nothing_lit(page):
    opened = _two_cycles(page)
    opened.canvas.node("b").setSelected(True)
    opened.open("other")

    assert _lit(opened.canvas) == set()


def test_a_lit_line_is_not_clipped_along_its_length(opened):
    """A path's own rect is the line through its middle and knows nothing
    about how thick it is drawn, so the margin has to cover the heaviest pen."""
    from cms_gui.cyclegraph import SPEC

    edge = [one for one in opened.canvas._edges
            if (one.source, one.target) == ("a", "b")][0]
    opened.canvas.node("b").setSelected(True)

    half = SPEC["edge"]["lit_width"] / 2.0
    drawn = edge.path().boundingRect().adjusted(-half, -half, half, half)
    assert edge.boundingRect().contains(drawn)


# --------------------------------------------- running one part of a cycle
# Working on one step of a twenty-step cycle meant running all twenty, which
# on this cycle means paying for six agent calls to reach the seventh.
def test_the_two_part_run_buttons_have_no_text_but_do_have_tooltips(opened):
    """They act on whichever step is selected: a label would have to name it
    or be vague."""
    for button in (opened.step_button, opened.from_button):
        assert button.text() == ""
        assert button.toolTip()
        assert not button.icon().isNull()


def test_they_say_where_the_other_steps_results_come_from(opened):
    """"Run one step" raises the question, so the tooltip answers it."""
    for button in (opened.step_button, opened.from_button):
        assert "last run" in button.toolTip()


def test_they_are_offered_only_once_a_step_is_picked(opened):
    assert opened.step_button.isEnabled() is False
    assert opened.from_button.isEnabled() is False

    opened.canvas.node("b").setSelected(True)

    assert opened.step_button.isEnabled() is True
    assert opened.from_button.isEnabled() is True


def test_letting_go_of_the_step_takes_them_away_again(opened):
    opened.canvas.node("b").setSelected(True)
    opened.canvas.scene().clearSelection()

    assert opened.step_button.isEnabled() is False


def test_one_chevron_asks_for_that_step_alone(opened):
    asked = []
    opened.run_requested.connect(lambda *one: asked.append(one))
    opened.canvas.node("b").setSelected(True)
    opened.step_button.click()

    assert asked == [("demo", "only", "b")]


def test_two_chevrons_ask_for_that_step_and_what_waits_on_it(opened):
    asked = []
    opened.run_requested.connect(lambda *one: asked.append(one))
    opened.canvas.node("c").setSelected(True)
    opened.from_button.click()

    assert asked == [("demo", "from", "c")]


def test_a_part_run_with_nothing_selected_asks_for_nothing(opened):
    """Belt and braces: the buttons are disabled, and the handler refuses
    anyway - a signal that named no step would run the whole cycle."""
    asked = []
    opened.run_requested.connect(lambda *one: asked.append(one))
    opened._run("only")

    assert asked == []


def test_the_page_never_spells_a_command_line_flag(opened):
    """It says what was asked for; how that is written is the core adapter's
    business, and a page that knew would be a second place to change."""
    import inspect

    from cms_gui.pages import cycles as page_module

    assert "--cycle-only" not in inspect.getsource(page_module)
    assert "--cycle-from" not in inspect.getsource(page_module)


# ------------------------------------------------------- asking first
# A full run of a real cycle spends agent calls and moves a Jira issue, and
# neither of those is undone by pressing Stop.
@pytest.fixture
def asked(monkeypatch):
    """Record what was asked, and refuse it."""
    from cms_gui import widgets

    questions = []

    def refuse(_parent, title, question, detail="", **k):
        questions.append((title, question, detail))
        return False

    monkeypatch.setattr(widgets, "confirm", refuse)
    return questions


def test_running_the_whole_cycle_asks_first(opened, asked):
    started = []
    opened.run_requested.connect(lambda *one: started.append(one))
    opened._run()

    assert len(asked) == 1
    assert "4" in asked[0][1], "it says how many steps that is"
    assert started == [], "and No means no"


def test_hard_run_explains_repeated_paid_calls_and_can_be_declined(opened, asked):
    requests = []
    opened.run_requested.connect(lambda *args: requests.append(args))
    opened.hard_run_action.trigger()
    assert asked[0][0] == "Hard Run"
    assert "beginning" in asked[0][1]
    assert "Paid services will be called again" in asked[0][2]
    assert requests == []


def test_running_from_a_step_asks_first(opened, asked):
    started = []
    opened.run_requested.connect(lambda *one: started.append(one))
    opened.canvas.node("b").setSelected(True)
    opened._run("from")

    assert len(asked) == 1
    assert "b" in asked[0][1]
    assert started == []


def test_running_from_a_step_says_how_much_it_will_run(opened, asked):
    """b, and d which waits on it - so one other step."""
    opened.canvas.node("b").setSelected(True)
    opened._run("from")

    assert "1 step(s)" in asked[0][1]
    assert "last run" in asked[0][2], "and where the rest comes from"


def test_running_one_step_does_not_ask(opened, asked):
    """The cheap, reversible one. These buttons exist so somebody can press
    them repeatedly while working on one part; a prompt would be in the way."""
    started = []
    opened.run_requested.connect(lambda *one: started.append(one))
    opened.canvas.node("b").setSelected(True)
    opened._run("only")

    assert asked == []
    assert started == [("demo", "only", "b")]


def test_agreeing_starts_it(opened):
    started = []
    opened.run_requested.connect(lambda *one: started.append(one))
    opened.canvas.node("b").setSelected(True)
    opened._run("from")

    assert started == [("demo", "from", "b")]


def test_counting_what_waits_on_a_step():
    from cms_gui.pages.cycles import _waiting_on

    graph = DIAMOND
    assert _waiting_on(graph, "a") == {"a", "b", "c", "d"}
    assert _waiting_on(graph, "b") == {"b", "d"}
    assert _waiting_on(graph, "d") == {"d"}
    assert _waiting_on(graph, "nobody") == set()


# --------------------------------------------------------- and visible
def test_the_part_run_buttons_are_not_ghosts(opened):
    """Ghost means a transparent border, which is right for the view controls
    beside them and wrong for a button that starts work."""
    for button in (opened.step_button, opened.from_button):
        assert button.property("variant") != "ghost"
    assert opened.fit_button.property("variant") == "ghost", "unlike these"


def test_the_warning_says_what_a_run_can_actually_do(opened, asked):
    """A confirmation that only asks "are you sure" tells somebody nothing
    they did not already know when they pressed the button."""
    opened._run()
    detail = asked[0][2]

    assert "this machine" in detail, "it can change the local machine"
    assert "outside it" in detail, "and reach systems that are not it"
    assert "cost money" in detail, "and spend money"


def test_the_warning_says_stop_does_not_undo(opened, asked):
    """The sentence that matters. Stop ends the run; it does not undo a
    commit, a transition, or a file an agent has already written."""
    opened._run()
    assert "does not undo" in asked[0][2]


def test_running_from_a_step_carries_the_same_warning(opened, asked):
    opened.canvas.node("b").setSelected(True)
    opened._run("from")

    from cms_gui.pages.cycles import CONSEQUENCE
    assert CONSEQUENCE in asked[0][2]


# --------------------------------------------- rebuilding under a live scene
def test_opening_another_cycle_while_a_step_is_selected(opened):
    """Clearing the scene deletes the items, and deleting a selected one emits
    selectionChanged - into a handler that walks the dictionary of items. Done
    in the wrong order it walks ones whose C++ half has just gone, which is a
    RuntimeError out of a signal handler rather than anything the method that
    caused it would lead you to expect."""
    opened.canvas.node("b").setSelected(True)
    opened.open("demo")                       # must not raise
    assert opened.canvas.node_ids()


def test_the_selection_is_asked_of_nothing_while_the_scene_is_being_replaced(
        opened):
    """The handler runs *during* the rebuild, when the old items are going and
    the new ones do not exist."""
    seen = []
    opened.canvas.node_selected.connect(seen.append)
    opened.canvas.node("b").setSelected(True)
    seen.clear()

    opened.open("demo")

    assert all(one == "" for one in seen), seen


def test_a_deleted_item_is_not_asked_whether_it_is_selected(opened):
    """Belt and braces: the scene can drop items from paths this class does
    not own, and asking one of those raises from inside a repaint."""
    import shiboken6

    item = opened.canvas.node("b")
    opened.canvas.scene().removeItem(item)
    shiboken6.delete(item)

    assert opened.canvas.selected() == ""     # must not raise


def test_clearing_the_graph_altogether_is_safe_with_a_selection(opened):
    opened.canvas.node("b").setSelected(True)
    opened.canvas.set_graph(None)

    assert opened.canvas.node_ids() == []
    assert opened.canvas.selected() == ""


# ------------------------------------------- what a step came to, not just said
def test_implementation_output_defaults_to_summary_and_keeps_raw_details(opened):
    from test_cycleoutput import failed_step

    state = RunState()
    opened.set_run_state(state)
    state.handle({"kind": "cycle.run.start", "run_id": "r", "cycle": "demo",
                  "graph": DIAMOND})
    state.handle({"kind": "cycle.step.start", "run_id": "r", "step": "a",
                  "plugin": "agent.implement"})
    step = failed_step()
    state.handle(dict(step, kind="cycle.step.end", run_id="r", step="a"))
    opened.canvas.select("a")
    settled()
    assert opened.output.output_views.currentWidget() is opened.output.summary
    assert "Agent reached its step limit" in opened.output.summary.toPlainText()
    assert "error_max_turns" not in opened.output.summary.toPlainText()
    opened.output.details_button.click()
    assert opened.output.output_views.currentWidget() is opened.output.log
    assert "error_max_turns" in opened.output.log.toPlainText()
    # Incoming events do not force the reader out of details.
    opened._paint_log()
    assert opened.output.output_views.currentWidget() is opened.output.log
    opened.output.details_button.click()
    assert opened.output.output_views.currentWidget() is opened.output.summary
    opened.canvas.select("b")
    assert opened.output.output_views.currentWidget() is opened.output.log
    assert opened.output.details_button.isHidden()
    opened.canvas.select("a")
    assert opened.output.output_views.currentWidget() is opened.output.summary
    opened.output.clear_log()
    assert opened.output.summary.toPlainText() == ""


# The panel used to show only what a step *printed*, which for half of them is
# nothing at all: a Jira step returns issues, a memory step returns what it
# remembered, and neither says a word on its way past. A run could finish,
# having done exactly what was asked, and leave the one place somebody looks
# blank.
def _account(step):
    from cms_gui.pages.cycles import _account_of
    return [line for _stream, line in _account_of(step)]


def test_a_step_that_printed_nothing_still_says_what_it_did():
    said = _account({"status": "success", "message": "jira: 1 issue, 1 comment",
                     "outputs": {"count": 1, "key": "QA-7"},
                     "artifacts": [{"path": "steps/todo/jira.json"}]})

    assert "-- success" in said
    assert "jira: 1 issue, 1 comment" in said
    assert any("count = 1" in one for one in said)
    assert any("key = QA-7" in one for one in said)
    assert any("steps/todo/jira.json" in one for one in said)


def test_a_step_still_running_is_not_summed_up_early():
    """Half an answer read as a whole one is worse than none."""
    for status in ("", "pending", "running", "waiting"):
        assert _account({"status": status, "message": "going"}) == []


def test_a_failure_says_so_and_why():
    said = _account({"status": "failed", "message": "there is a task: '0' > '0'",
                     "outputs": {"passed": False}})

    assert "-- failed" in said
    assert "there is a task: '0' > '0'" in said


def test_one_enormous_output_does_not_take_the_panel_over():
    """A step can carry a whole Jira issue in one output. The panel is for
    "what happened"; the record on disk is for the rest."""
    from cms_gui.pages.cycles import OUTPUT_CHARS

    said = _account({"status": "success", "outputs": {"issues": "x" * 5000}})
    longest = max(len(one) for one in said)

    assert longest < OUTPUT_CHARS + 40
    assert any(one.endswith("...") for one in said)


def test_an_output_spread_over_lines_is_shown_on_one():
    said = _account({"status": "success", "outputs": {"body": "a\nb\nc"}})
    assert any("body = a b c" in one for one in said)


def test_it_follows_what_the_step_actually_printed(opened):
    """Appended, not instead of: a step with real output keeps it, and the
    account closes it off."""
    opened.run_state = RunState()
    opened.run_state.handle({"kind": "cycle.run.start", "cycle": "demo",
                             "graph": DIAMOND, "jobs": 1})
    opened.run_state.handle({"kind": "cycle.step.log", "step": "a",
                             "stream": "out", "lines": ["working"]})
    opened.run_state.handle({"kind": "cycle.step.end", "step": "a",
                             "status": "success", "message": "exited 0",
                             "outputs": {"exit_code": 0}})
    opened._selected = "a"
    opened._paint_log()

    shown = opened.output.log.toPlainText().splitlines()
    assert shown[0] == "working"
    assert "-- success" in shown
    assert "exited 0" in shown


def test_the_narrow_inspector_shortener_is_not_the_panel_s(qapp):
    """Two things called _short, the later one quietly winning: the Inspector
    wants 60 characters in a narrow column, the panel wants a readable line."""
    from cms_gui.pages import cycles as page

    assert page.OUTPUT_CHARS > 60
    assert len(page._one_line("x" * 500)) > 60
    assert len(page._short("x" * 500)) <= 64


# ------------------------------------------------------------------ subjects
def _session(key="QA-1", run_id="r1", cycle="demo", pin="task_key", running=False):
    return {"id": "%s:%s" % (cycle, key), "cycle": cycle, "name": "Demo cycle",
            "subject": {"kind": "task", "key": key, "title": "Fix login",
                        "memory": "dev/" + key, "pin": pin},
            "status": "failed", "running": running, "reached": "b",
            "reached_label": "B", "reached_plugin": "", "message": "",
            "latest": run_id, "started_at": 1.0, "updated_at": 2.0,
            "memory": {}, "runs": [{"id": run_id, "run_dir": "/runs/" + run_id,
                                    "started_at": 1.0, "state": "failed"}]}


def test_the_sessions_are_read_once_the_page_opens_a_cycle(page):
    core = FakeCore({"demo": payload()})
    core.sessions = [_session()]
    page.set_core(core)
    page.set_inventory(FakeInventory([row()], [SHELL_PLUGIN]))
    page.open("demo")
    assert core.sessions_asked == 1
    assert page.subjects.row_ids() == ["new", "demo:QA-1"]


def test_a_run_that_finds_its_subject_says_so_in_the_header(opened):
    state = RunState()
    opened.set_run_state(state)
    state.handle({"kind": "cycle.run.start", "run_id": "r1", "cycle": "demo",
                  "graph": DIAMOND})
    state.handle({"kind": "cycle.subject", "run_id": "r1",
                  "subject": {"kind": "task", "key": "QA-1", "step": "a"}})
    settled()
    assert "working on task QA-1" in opened.state.text()


def test_picking_a_session_asks_for_its_latest_run(opened):
    opened.core.sessions = [_session()]
    opened.refresh_sessions()
    asked = []
    opened.session_opened.connect(lambda *args: asked.append(args))
    opened.subjects.activated.emit(_session())
    assert asked == [("demo", "/runs/r1")]


def test_no_other_run_is_opened_while_a_cycle_is_going(opened, monkeypatch):
    from cms_gui import widgets
    monkeypatch.setattr(widgets, "note", lambda *a, **k: None)
    state = RunState()
    opened.set_run_state(state)
    state.handle({"kind": "cycle.run.start", "run_id": "live", "cycle": "demo",
                  "graph": DIAMOND})
    asked = []
    opened.session_opened.connect(lambda *args: asked.append(args))
    opened.subjects.activated.emit(_session())
    assert asked == []


def test_deleting_the_session_on_screen_clears_the_screen(opened):
    opened.core.sessions = [_session()]
    state = RunState()
    opened.set_run_state(state)
    state.handle({"kind": "cycle.run.start", "run_id": "r1", "cycle": "demo",
                  "graph": DIAMOND})
    state.handle({"kind": "cycle.run.end", "run_id": "r1", "status": "failed"})
    opened.refresh_sessions()
    opened.subjects.delete_requested.emit(_session())
    assert opened.core.sessions_deleted == ["demo:QA-1"]
    assert state.cycle is None
    assert opened.subjects.row_ids() == ["new"]


def test_a_refused_delete_is_said_and_keeps_the_session(opened, monkeypatch):
    from cms_gui import widgets
    said = []
    monkeypatch.setattr(widgets, "warn", lambda *a, **k: said.append(a))
    opened.core.sessions = [_session()]
    opened.refresh_sessions()
    opened.core.cycle_session_delete = lambda _id: {
        "ok": False, "problems": ["demo:QA-1 is still running"]}
    opened.subjects.delete_requested.emit(_session())
    assert said and "still running" in said[0][2]
    assert "demo:QA-1" in opened.subjects.row_ids()
