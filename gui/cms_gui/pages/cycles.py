"""Cycles: the whole of one, at once, and what it does when it runs.

A **cycle** is a graph of steps - start a service, run scenarios, run a command,
write a report - and the steps that do not depend on each other run at the same
time. Scenarios are what this application drives a browser through; a cycle is
what arranges an afternoon's worth of them around the services they need.

The page is shaped the way somebody works: the project's cycles on the left,
the one that is open in the middle as the graph it actually is, and whatever is
selected on the right. Opening a cycle shows **all of it** - that is the point
of drawing it rather than listing it, and a list of steps would hide precisely
the thing a cycle has that a scenario does not, which is shape.

**The same canvas is the run view.** Starting a cycle does not open another
page: the nodes already on screen take on their live status. There is no second
picture of a run to keep in step with this one, because there is no second
picture.

Nothing here parses YAML, and nothing here knows what a plugin does. The page
asks the core over --cycle-list / --cycle-show, draws the graph the core hands
back, and builds a step's fields from the plugin metadata --describe
publishes. A plugin added to the core appears here with no change to this file.

**What the cycles work on** sits to the right of the canvas: one row per
session - every run of one cycle on one subject, a Jira task say - so it is
plain which task a run is about, and going back to one is a click. See
``cms_gui.cyclesubjects``.
"""

import os
import subprocess

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QInputDialog,
                               QLineEdit, QMenu, QPlainTextEdit,
                               QPushButton, QStackedWidget, QTabWidget,
                               QTextBrowser, QToolButton, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import (core as core_mod, cyclelayoutfile as clf,
                cycleprojectsfile as cpf, icons, theme, widgets)
from ..cyclegraph import STATUS_LOOK, CycleCanvas
from ..cycleoutput import implementation_html
from ..cyclesubjects import SPEC as SUBJECTS_SPEC, SubjectsPanel
from ..stages import StageList
from .cycledialogs import CycleDialog, NodeDialog

#: Roles on an explorer row. Qt's user role plus one, so the two things a row
#: has to carry - what kind of row it is and which cycle it names - do not have
#: to be encoded into its text.
ROLE_KIND = Qt.UserRole
ROLE_ID = Qt.UserRole + 1

#: How long the canvas has to sit still before where it is looking is written
#: down. Long enough that a wheel spun through half a dozen steps, or a hand
#: dragging the canvas across the page, is one write rather than a hundred;
#: short enough that closing the application straight after arriving somewhere
#: keeps it.
VIEW_SETTLED_MS = 800

#: How the splitters divide the page when nothing has been dragged yet.
EXPLORER_WIDTH = 240
SUBJECTS_WIDTH = SUBJECTS_SPEC["width"]
OUTPUT_HEIGHT = 170

#: The output panel's tabs, by position. Named because three places set a tab's
#: text by index and a fourth tab inserted in the middle would otherwise rename
#: the wrong one - which is exactly what happened when Stages was added.
TAB_OUTPUT, TAB_STAGES, TAB_PROBLEMS, TAB_YAML = range(4)

#: Lines of a step's output painted at once. The model keeps more (see
#: cms_gui.runner.CYCLE_LOG_LINES); this is only what is worth re-rendering.
VISIBLE_LINES = 400

#: How far off the bottom of the output still counts as being at it.
LOG_SLACK = 2

#: What the group holding cycles that name no project - or one nothing knows -
#: is called. Named rather than hidden, the way the Services page handles a log
#: whose stack has gone: losing sight of a cycle because its project was
#: renamed would be the worst possible answer.
UNASSIGNED = "Unassigned"

#: The starting point a new cycle is written with. It has to compile - the core
#: refuses to write anything that does not - so it is one real step rather than
#: an empty file somebody then has to make valid before it can be saved again.
STARTER_STEP = {"id": "first", "plugin": "command.shell",
                "with": {"command": "echo hello"}}

#: What somebody is agreeing to when they start a cycle. Said plainly, because
#: a confirmation that only asks "are you sure" tells them nothing they did not
#: already know when they pressed the button - and what a cycle can do is not
#: obvious from a graph of boxes.
#:
#: The last sentence is the one that matters. Stop ends the run; it does not
#: undo a commit, a transition, or a file an agent has already written.
CONSEQUENCE = (
    "A cycle's steps run on this machine and can change it - files, "
    "repositories, services it starts - and can reach systems outside it, "
    "such as an issue tracker, using the credentials it is configured with. "
    "Agent steps cost money.\n\n"
    "Stop ends the run. It does not undo what has already happened.")


class CyclesPage(QWidget):
    """Explorer and canvas - and the run, painted on that same canvas.

    A step is edited in a window of its own rather than in a panel down the
    side: a step has a dozen fields plus whatever its plugin declares, and a
    column three hundred pixels wide made every one of them cramped while the
    canvas gave up a third of its width to something only wanted occasionally.
    """

    #: cycle id, mode ("" | "only" | "from" | "resume"), and step or run id. The
    #: page says what was asked for; how that is spelled on a command line is
    #: the core adapter's business, not a form's.
    run_requested = Signal(str, str, str)
    cycle_opened = Signal(str)
    #: Put this earlier run on the canvas: (cycle id, run directory).
    session_opened = Signal(str, str)
    #: A planned task, to start work on: the window knows how to run it.
    planned_started = Signal(dict)
    stop_requested = Signal()
    saved = Signal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.core = None
        self.inventory = None
        self.run_state = None
        self._cycles = []                # rows from --describe
        self._projects = []              # cycleprojectsfile.ProjectRow
        self._projects_path = ""
        self._open = None                # the --cycle-show payload, or None
        self._selected = ""              # step id selected on the canvas
        # Repaints asked for but not yet done - see _schedule_paint.
        self._paint_pending = False
        self._buttons_pending = False
        self._sessions_pending = False
        self._sessions_loaded = False
        self._build()
        self.load_projects()

    # -- construction ---------------------------------------------------------
    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(12)

        self.run_button = icons.button(QToolButton(), "run", "Run")
        self.run_button.setProperty("variant", "primary")
        self.run_button.setProperty("hasmenu", "true")
        self.run_button.setPopupMode(QToolButton.MenuButtonPopup)
        self.run_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        # clicked(bool) is the button state, not the textual run mode.
        self.run_button.clicked.connect(
            lambda: self._run("resume" if self._resume_available() else ""))
        self.run_menu = QMenu(self.run_button)
        self.run_menu.setToolTipsVisible(True)
        self.hard_run_action = self.run_menu.addAction("Hard Run")
        self.hard_run_action.setToolTip(
            "Start the entire cycle from the beginning in a new run.\n"
            "Paid services will be called again; previous runs remain saved.")
        self.hard_run_action.triggered.connect(lambda: self._run("hard"))
        self.run_button.setMenu(self.run_menu)
        # No text on these two: they act on whichever step is selected, and a
        # label would have to name it or be vague. The tooltip says both what
        # it does and what it borrows, because "run one step" raises the
        # question of where the others' results come from.
        self.step_button = icons.button(QPushButton(), "run-step")
        self.step_button.setToolTip(
            "Run only the selected step.\n"
            "Reuse the displayed run, or the last run of this cycle if none is displayed.")
        self.step_button.clicked.connect(lambda: self._run("only"))
        self.from_button = icons.button(QPushButton(), "run-from")
        self.from_button.setToolTip(
            "Run the selected step and everything that waits on it.\n"
            "Reuse the displayed run, or the last run of this cycle if none is displayed.")
        self.from_button.clicked.connect(lambda: self._run("from"))
        self.stop_button = icons.button(QPushButton(), "stop", "Stop")
        self.stop_button.clicked.connect(self.stop_requested.emit)
        self.stop_button.setEnabled(False)
        self.properties_button = QPushButton("Properties")
        self.properties_button.setToolTip(
            "The cycle's name, project and variables.")
        self.properties_button.clicked.connect(self.open_properties)
        self.arrange_button = icons.button(QPushButton(), "refresh", "Arrange")
        self.arrange_button.setToolTip(
            "Put every step back where the layout says it belongs.\n"
            "Nothing about moving a node is permanent.")
        self.fit_button = QPushButton("Fit")
        self.fit_button.setToolTip("Show all of it.")
        self.zoom_out_button = icons.button(QPushButton(), "minus")
        self.zoom_in_button = icons.button(QPushButton(), "plus")
        for button in (self.properties_button, self.arrange_button,
                       self.fit_button, self.zoom_out_button,
                       self.zoom_in_button):
            button.setProperty("variant", "ghost")
        # Not ghost, deliberately. Ghost means a transparent border, which is
        # right for the view controls beside them and wrong for these: they
        # start work, the way Run and Stop do, and a button that starts
        # something should look like a button.

        self.title = widgets.elided_line("Cycles")
        self.state = widgets.elided_line("", "mono")

        layout.addWidget(widgets.row(
            self.title, None, self.state, self.properties_button,
            self.arrange_button, self.zoom_out_button, self.zoom_in_button,
            self.fit_button, self.step_button, self.from_button,
            self.stop_button, self.run_button))
        layout.addWidget(widgets.lede(
            "A cycle runs services, scenarios, commands and reports as one "
            "graph. Steps that do not depend on each other run at the same "
            "time. Pick one on the left to see all of it - drag the canvas to "
            "move around it, drag a step to shift it, and Arrange puts them "
            "all back. Double-click a step to open it."))
        self.run_hint = widgets.lede("")
        layout.addWidget(self.run_hint)

        self.canvas = CycleCanvas(self)
        self.canvas.node_selected.connect(self._node_selected)
        self.canvas.node_opened.connect(self.open_step)
        self.canvas.nodes_moved.connect(self._remember_places)
        self.canvas.view_changed.connect(self._view_moved)
        self.canvas.view_reset.connect(self._view_reset)
        # What makes a zoom and a pan cheap to remember: the canvas says it
        # moved as often as it likes, and the file is written once the moving
        # has stopped. See _view_moved.
        self._forget_view = False
        self._view_timer = QTimer(self)
        self._view_timer.setSingleShot(True)
        self._view_timer.setInterval(VIEW_SETTLED_MS)
        self._view_timer.timeout.connect(self._remember_view)
        self.arrange_button.clicked.connect(self.canvas.arrange)
        self.fit_button.clicked.connect(self.canvas.fit)
        self.zoom_in_button.clicked.connect(self.canvas.zoom_in)
        self.zoom_out_button.clicked.connect(self.canvas.zoom_out)

        self.explorer = _Explorer(self)
        self.explorer.opened.connect(self.open)
        self.explorer.project_added.connect(self._new_project)
        self.explorer.project_renamed.connect(self._rename_project)
        self.explorer.project_deleted.connect(self._delete_project)
        self.explorer.cycle_added.connect(self._new_cycle)
        self.explorer.cycle_duplicated.connect(self._duplicate_cycle)
        self.explorer.cycle_renamed.connect(self._rename_cycle)
        self.explorer.cycle_reidentified.connect(self._change_cycle_id)
        self.explorer.cycle_deleted.connect(self._delete_cycle)
        self.explorer.cycle_imported.connect(self._import_cycle)
        self.output = _Output(self)
        self.output.save_requested.connect(self._save_yaml)
        self.subjects = SubjectsPanel(self)
        self.subjects.activated.connect(self._open_session)
        self.subjects.run_picked.connect(self._open_session_run)
        self.subjects.new_requested.connect(lambda: self._run(""))
        self.subjects.delete_requested.connect(self._delete_session)
        self.subjects.planned_start.connect(self.planned_started.emit)
        self.subjects.planned_remove.connect(self._remove_planned)

        # Folding splitters: any panel dragged shut leaves a bold line where
        # it went, and clicking the line brings it back.
        across = widgets.FoldingSplitter(Qt.Horizontal)
        across.addWidget(self.explorer)
        across.addWidget(_framed(self.canvas))
        across.addWidget(self.subjects)
        across.setStretchFactor(1, 1)
        across.setSizes([EXPLORER_WIDTH, 900, SUBJECTS_WIDTH])
        across.remember_in(self.settings, "cycles/across")

        down = widgets.FoldingSplitter(Qt.Vertical)
        down.addWidget(across)
        down.addWidget(self.output)
        down.setStretchFactor(0, 1)
        down.setSizes([500, OUTPUT_HEIGHT])
        down.remember_in(self.settings, "cycles/down")
        layout.addWidget(down, 1)

        self._update_buttons()

    # -- what the window gives it --------------------------------------------
    def set_core(self, core):
        self.core = core

    def set_inventory(self, inventory):
        self.inventory = inventory
        self._cycles = list(inventory.cycles) if inventory else []
        self.explorer.fill(self._cycles, self._projects)
        if getattr(self, "_pending_open", ""):
            self._open_pending()
        elif self._open:
            # Keep whatever was open across a refresh, so saving a scenario
            # elsewhere does not close the cycle somebody was reading.
            self.open(self._open.get("id", ""))
        self._update_buttons()

    def set_run_state(self, run_state):
        """Follow a live run. Connected once; the window owns the model."""
        self.run_state = run_state
        run_state.cycle_graph_known.connect(self._graph_known)
        run_state.cycle_step_changed.connect(self._step_changed)
        # A run found its subject, said how it began, was sent back, or
        # ended: the list on the right has something new to say.
        run_state.cycle_subject_changed.connect(self._subject_changed)
        # Coalesced: changed fires on every event of every run, including ones
        # that have nothing to do with a cycle.
        run_state.changed.connect(self._schedule_buttons)

    def set_projects_path(self, path):
        """Where the projects file lives. Set from Settings, through the window."""
        self._projects_path = cpf.resolve_path(path)
        self.load_projects()
        self.explorer.fill(self._cycles, self._projects)

    def load_projects(self):
        """Read the projects file. A missing one is simply no projects yet."""
        self._projects_path = self._projects_path or cpf.resolve_path(
            getattr(self.settings, "cycle_projects_path", ""))
        try:
            self._projects = cpf.load(self._projects_path)
        except cpf.CycleProjectsFileError as exc:
            self._projects = []
            self.output.set_problems([str(exc)])

    def _write_projects(self):
        """Save the projects file; returns True when it went."""
        try:
            cpf.save(self._projects_path, self._projects)
        except cpf.CycleProjectsFileError as exc:
            widgets.warn(self, "Projects", str(exc))
            return False
        self.explorer.fill(self._cycles, self._projects)
        return True

    def set_developer_mode(self, enabled):
        self.explorer.set_developer_mode(enabled)

    def handle_event(self, event):
        """Anything ``cycle.*`` from the launcher. The model does the work."""
        # Deliberately thin: RunState already turned the stream into a model,
        # and a second interpretation of the same events here is how two views
        # of one run start disagreeing.

    # -- opening one ----------------------------------------------------------
    def open(self, cycle_id):
        """Read one cycle and draw all of it."""
        if not cycle_id or self.core is None:
            return
        try:
            payload = self.core.cycle_show(cycle_id)
        except core_mod.CoreError as exc:
            self._open = None
            self.canvas.set_graph(None)
            self.output.set_problems([str(exc)])
            self.state.setText("could not read %s" % cycle_id)
            return

        # Which cycle is on the canvas, asked before the new payload replaces
        # the answer - and a view still waiting to be written down settled
        # while it is still clear which cycle it belongs to.
        self.cycle_opened.emit(cycle_id)
        drawn = self._drawn_id()
        self._flush_view()
        self._open = payload
        writable = bool(payload.get("writable"))
        self.title.setText(_name_of(payload) or "Cycles")
        # The same cycle, unchanged, is not a reason to rebuild anything. A
        # click on the cycle already open, a Refresh, closing Settings and
        # saving something on another page all land here, and rebuilding threw
        # away the statuses of a run that was still going, the selection, and
        # wherever the canvas had been zoomed and dragged to.
        graph = payload.get("cycle")
        if cycle_id == drawn and _same_shape(graph, self.canvas.graph()):
            self.canvas.update_graph(graph)
        else:
            # Where somebody last put the nodes, and where they were last
            # looking. Anything this does not mention - a step added since, or
            # a cycle never arranged - goes where the layout computes, so a
            # remembered arrangement never has to be thrown away.
            self.canvas.set_graph(graph, self._remembered(cycle_id),
                                  self._remembered_view(cycle_id))
            self._selected = ""
        # The canvas is drawn from the file, which knows nothing about a run.
        # Whatever the model knows about this cycle is painted back on, so a
        # run still going - or one that finished while somebody was elsewhere -
        # is still there to read.
        self._apply_run_state()
        # And what it said, not only what it did. Without this, a run restored
        # after a restart came back as a coloured graph beside an empty panel:
        # the model had the stages and the output, and nothing asked for them.
        self._paint_log()
        self.output.set_problems(payload.get("problems") or [])
        self.output.set_yaml(payload.get("yaml") or "", writable)
        self.explorer.select(cycle_id)
        self.subjects.set_cycle(cycle_id, graph)
        self.subjects.set_live(self._live_cycle())
        if not self._sessions_loaded:
            self.refresh_sessions()
        self._describe_state(payload)
        self._update_buttons()

    def _apply_run_state(self):
        """Paint what the run model knows onto the canvas, if it is this cycle.

        The statuses live on the node items and nowhere else, so every rebuild
        used to lose them while ``RunState`` still held the truth - the next
        event repainted the one step that moved and left the rest blank.
        """
        state = (self.run_state.cycle or {}) if self.run_state else {}
        if not state:
            return
        drawn = (self.canvas.graph() or {}).get("id") or self.current_id()
        if state.get("cycle") and drawn and state.get("cycle") != drawn:
            return
        for step_id, step in (state.get("steps") or {}).items():
            self.canvas.set_status(step_id, step.get("status"),
                                   step.get("duration_ms"), step.get("attempt"))

    def open_step(self, step_id):
        """Open one step for editing. Double-clicking a node is what calls this."""
        document = (self._open or {}).get("document")
        if not document or not step_id:
            return
        step = self._step_of(step_id)
        if step is None:
            return
        plugin = (self.inventory.cycle_plugin(step.get("plugin", ""))
                  if self.inventory else {})
        state = (self.run_state.cycle or {}) if self.run_state else {}

        dialog = NodeDialog(
            step, plugin, self, bool((self._open or {}).get("writable")),
            live=(state.get("steps") or {}).get(step_id),
            ask_plugin=lambda key, settings: self._ask_plugin(
                step.get("plugin", ""), key, settings),
            trigger=self._trigger_of(step_id))
        if dialog.exec() != QDialog.Accepted or dialog.saved is None:
            return
        self._replace_step(step_id, dialog.saved, dialog.saved_trigger)

    def _trigger_of(self, step_id):
        """The trigger that watches this step, as the file has it, or None."""
        document = (self._open or {}).get("document") or {}
        for one in document.get("triggers") or []:
            if isinstance(one, dict) and one.get("watch") == step_id:
                return one
        return None

    def open_properties(self):
        """The cycle's own name, project and variables."""
        document = (self._open or {}).get("document")
        if not document:
            return
        cycle_id = self.current_id()
        dialog = CycleDialog(document, cpf.names(self._projects), self,
                             bool((self._open or {}).get("writable")),
                             stored_secrets=self._stored_secrets(cycle_id))
        if dialog.exec() != QDialog.Accepted or dialog.saved is None:
            return
        changed = dict(document)
        for key, value in dialog.saved.items():
            if value:
                changed[key] = value
            else:
                changed.pop(key, None)
        if not self._write_cycle(cycle_id, changed):
            return
        # After the file, never before: a value put in the store for a cycle the
        # core then refused to write would be a credential belonging to nothing.
        problems = self._write_secrets(cycle_id, dialog.secret_values,
                                       dialog.dropped_secrets)
        self.open(cycle_id)
        if problems:
            # After the reopen, which sets the cycle's own problems and would
            # otherwise wipe these.
            self.output.set_problems(problems)

    def _stored_secrets(self, cycle_id):
        """Which of this cycle's secrets already have a value. Names only.

        The values themselves are never asked for and never sent back: all the
        form needs is whether a box should say "stored" or "not set".
        """
        if self.core is None or not cycle_id:
            return ()
        try:
            answer = self.core.cycle_secrets(cycle_id)
        except core_mod.CoreError:
            return ()
        return tuple(answer.get("secrets") or ())

    def _write_secrets(self, cycle_id, values, dropped):
        """Put the edited secrets in the store, take the dead ones out.

        Returns whatever went wrong, as lines. Every write is attempted even
        when one fails: a second secret is not made any safer by not being
        saved because the first could not be.
        """
        if self.core is None or not cycle_id:
            return []
        problems = []
        for name, value in sorted(values.items()):
            problems.extend(self._one_secret(
                lambda: self.core.cycle_secret_set(cycle_id, name, value), name))
        for name in dropped:
            problems.extend(self._one_secret(
                lambda: self.core.cycle_secret_delete(cycle_id, name), name))
        return problems

    def _one_secret(self, call, name):
        """One store write, with whatever went wrong as text rather than a raise."""
        try:
            answer = call()
        except core_mod.CoreError as exc:
            return ["%s: %s" % (name, exc)]
        if not answer.get("ok"):
            return ["%s: %s" % (name, "; ".join(answer.get("problems")
                                                or ["the core refused it"]))]
        return []

    def _step_of(self, step_id):
        for step in ((self._open or {}).get("document") or {}).get("steps") or []:
            if step.get("id") == step_id:
                return step
        return None

    def _replace_step(self, step_id, changed, listening=None):
        """Put an edited step back in the document and write the whole thing.

        The whole document rather than a patch: the core writes a cycle file,
        not a field, and handing it what is on screen keeps one shape going in
        and out.
        """
        document = dict((self._open or {}).get("document") or {})
        steps = [dict(one) for one in document.get("steps") or []]
        for index, step in enumerate(steps):
            if step.get("id") == step_id:
                steps[index] = changed
                break
        else:
            return
        document["steps"] = steps
        document = _with_listening(document, step_id, changed.get("id", step_id),
                                   listening)
        cycle_id = self.current_id()
        if self._write_cycle(cycle_id, document):
            self.open(cycle_id)

    def _ask_plugin(self, plugin_id, action_key, settings):
        """Put one of a plugin's declared actions to the core."""
        if self.core is None:
            return {"ok": False, "summary": "No core configured", "argv": []}
        return self.core.cycle_plugin_action(plugin_id, action_key, settings)

    def current_id(self):
        return (self._open or {}).get("id", "")

    def hideEvent(self, event):             # noqa: N802 - Qt's own spelling
        """Going somewhere else - or closing - settles a pending view.

        The wait before writing one down is what keeps a wheel cheap; it must
        not also mean that zooming and then leaving the page loses it.
        """
        self._flush_view()
        super().hideEvent(event)

    def _open_pending(self):
        """Open whatever a write asked to be opened, once the tree is rebuilt."""
        pending, self._pending_open = getattr(self, "_pending_open", ""), ""
        if pending:
            self.open(pending)

    def _describe_state(self, payload):
        steps = len((payload.get("cycle") or {}).get("nodes") or [])
        problems = payload.get("problems") or []
        where = "" if payload.get("writable") else " - ships with the app"
        subject = (self._live_cycle() or {}).get("subject") or {}
        if subject.get("key"):
            where += " - working on %s %s" % (subject.get("kind") or "",
                                              subject["key"])
        if problems:
            self.state.setText("%d step(s), %d problem(s)%s"
                               % (steps, len(problems), where))
        else:
            self.state.setText("%d step(s)%s" % (steps, where))

    # -- sessions: what the cycles work on --------------------------------------
    def _live_cycle(self):
        """The run on screen, when it is a run of the open cycle."""
        state = (self.run_state.cycle or {}) if self.run_state else {}
        if state and state.get("cycle") == self.current_id():
            return state
        return None

    def refresh_sessions(self):
        """Ask the core for the sessions again. Never raises.

        A list that could not be read is left as it was, not a dialog: this
        runs on its own whenever a run moves, and a dialog nobody asked for,
        repeated on every step, would be worse than a list a moment stale.
        """
        if self.core is None:
            return
        try:
            answer = self.core.cycle_sessions()
        except core_mod.CoreError:
            return
        self._sessions_loaded = True
        self.subjects.set_sessions((answer or {}).get("sessions") or [])
        self.refresh_planned()
        self.subjects.set_live(self._live_cycle())
        self._update_buttons()

    def refresh_planned(self):
        """Ask the core what is on the plan. Never raises, for the same reason
        refresh_sessions does not."""
        if self.core is None:
            return
        try:
            answer = self.core.cycle_watch_planned()
        except core_mod.CoreError:
            return
        self.subjects.set_planned((answer or {}).get("planned") or [])

    def _remove_planned(self, item):
        """Off the plan, and not offered again."""
        if self.core is None:
            return
        try:
            self.core.cycle_plan_remove(item.get("id", ""))
        except core_mod.CoreError as exc:
            self.output.set_problems([str(exc)])
        self.refresh_planned()

    def _schedule_sessions(self):
        if not self._sessions_pending:
            self._sessions_pending = True
            QTimer.singleShot(0, self._flush_sessions)

    def _flush_sessions(self):
        self._sessions_pending = False
        self.refresh_sessions()

    def _subject_changed(self):
        self.subjects.set_live(self._live_cycle())
        if self._open:
            self._describe_state(self._open)
        self._schedule_sessions()

    def _may_leave_the_run(self):
        """Whether the run on screen may be swapped for another. Says why not."""
        if self.run_state and self.run_state.cycle_running:
            widgets.note(self, "Subjects",
                         "A cycle is running. Another run can be opened once "
                         "it has finished or been stopped.")
            return False
        return True

    def _open_session(self, session):
        """Put a session's latest run on the canvas, so Resume acts on it."""
        runs = session.get("runs") or []
        live = self._live_cycle() or {}
        if not runs or any(one.get("id") == live.get("run_id") for one in runs[:1]):
            if session.get("cycle") != self.current_id():
                self.open(session.get("cycle", ""))
            return
        if not self._may_leave_the_run():
            return
        self.session_opened.emit(session.get("cycle", ""), runs[0].get("run_dir", ""))

    def _open_session_run(self, cycle_id, run_id):
        for session in self.subjects.sessions():
            for one in session.get("runs") or []:
                if one.get("id") == run_id and session.get("cycle") == cycle_id:
                    if self._may_leave_the_run():
                        self.session_opened.emit(cycle_id, one.get("run_dir", ""))
                    return

    def _delete_session(self, session):
        """Delete one session, after saying exactly what goes and what stays."""
        if self.core is None or session.get("running"):
            return
        runs = session.get("runs") or []
        subject = session.get("subject") or {}
        detail = ["%d run(s) and everything they wrote - logs, reports, "
                  "artifacts." % len(runs)]
        if (session.get("memory") or {}).get("key"):
            detail.append("What the cycle remembers about it (%s). That includes "
                          "its attempt count and whether it was done, so the "
                          "cycle may take it up again." % session["memory"]["key"])
        detail.append("Branches, commits and issues are not touched.")
        if not widgets.confirm(
                self, "Delete session",
                "Delete %s?" % (subject.get("key") or "this run"),
                "\n\n".join(detail), agree="Delete", refuse="Keep"):
            return
        try:
            answer = self.core.cycle_session_delete(session.get("id", ""))
        except core_mod.CoreError as exc:
            widgets.warn(self, "Delete session", str(exc))
            return
        if not (answer or {}).get("ok", True):
            widgets.warn(self, "Delete session",
                         "\n".join((answer or {}).get("problems") or ["It was not deleted."]))
            return
        live = (self.run_state.cycle or {}) if self.run_state else {}
        if any(one.get("id") == live.get("run_id") for one in runs):
            # What is on screen was just deleted; showing it would be showing
            # a run that no longer exists anywhere.
            self.run_state.reset_cycle()
            self.canvas.clear_status()
            self.output.clear_log()
        self.refresh_sessions()

    # -- projects -------------------------------------------------------------
    # The projects file is the page's alone, so these are the only writers of
    # it. Which cycles belong to a project is not recorded there: each cycle
    # says so itself, which is what makes a cycle file mean the same thing on
    # another machine.

    def _new_project(self):
        name = self._ask("New project", "Name:")
        if not name:
            return
        if cpf.find(self._projects, name) is not None:
            widgets.note(self, "New project",
                                    "There is already a project called %r." % name)
            return
        import datetime
        self._projects.append(cpf.ProjectRow(
            name=name, added=datetime.date.today().isoformat()))
        if self._write_projects():
            # Selected, so Rename and Delete have something to act on the
            # moment it appears - which is when somebody is most likely to
            # want them.
            self.explorer.select_project(name)

    def _rename_project(self, old):
        project = cpf.find(self._projects, old)
        if project is None:
            return
        name = self._ask("Rename project", "Name:", old)
        if not name or name == old:
            return
        if cpf.find(self._projects, name) is not None:
            widgets.note(self, "Rename project",
                                    "There is already a project called %r." % name)
            return

        moving = [row for row in self._cycles if row.get("project") == old]
        if moving and not self._confirm(
                "Rename project",
                "%d cycle(s) name %r. They will be rewritten to name %r."
                % (len(moving), old, name)):
            return

        project.name = name
        if not self._write_projects():
            return
        self.explorer.select_project(name)
        # The cycles are rewritten rather than left pointing at a name that no
        # longer exists - otherwise a rename quietly moves every one of them to
        # Unassigned, which is the Services page's lesson about renaming a stack
        # behind its logs' backs.
        failed = [row["id"] for row in moving
                  if not self._set_cycle_project(row["id"], name)]
        if failed:
            widgets.warn(self, "Rename project",
                                "Renamed, but these could not be rewritten and "
                                "now show as unassigned:\n\n- %s"
                                % "\n- ".join(failed))
        self.saved.emit()

    def _delete_project(self, name):
        project = cpf.find(self._projects, name)
        if project is None:
            return
        orphans = [row for row in self._cycles if row.get("project") == name]
        extra = ("\n\n%d cycle(s) name it. The files are kept and move to %s."
                 % (len(orphans), UNASSIGNED)) if orphans else ""
        if not self._confirm("Delete project",
                             "Delete the project %r?%s" % (name, extra)):
            return
        self._projects = [one for one in self._projects if one is not project]
        self._write_projects()

    def _set_cycle_project(self, cycle_id, project):
        """Rewrite one cycle's ``project:``. True when it was written."""
        if self.core is None:
            return False
        try:
            payload = self.core.cycle_show(cycle_id)
        except core_mod.CoreError:
            return False
        document = payload.get("document")
        if not document or not payload.get("writable"):
            return False
        if project:
            document["project"] = project
        else:
            document.pop("project", None)
        try:
            written = self.core.cycle_save(cycle_id, document)
        except core_mod.CoreError:
            return False
        return bool(written.get("ok"))

    # -- cycles ---------------------------------------------------------------
    def _new_cycle(self, project=""):
        cycle_id = self._ask("New cycle", "Id (letters, digits, _ and -):")
        if not cycle_id:
            return
        if any(row.get("id") == cycle_id for row in self._cycles):
            widgets.note(self, "New cycle",
                                    "There is already a cycle called %r."
                                    % cycle_id)
            return
        document = {"id": cycle_id, "name": cycle_id.replace("_", " ").title(),
                    "steps": [dict(STARTER_STEP)]}
        if project:
            document["project"] = project
        self._write_cycle(cycle_id, document, then_open=True)

    def _duplicate_cycle(self, cycle_id):
        try:
            payload = self.core.cycle_show(cycle_id)
        except core_mod.CoreError as exc:
            widgets.warn(self, "Duplicate", str(exc))
            return
        document = payload.get("document")
        if not document:
            widgets.note(self, "Duplicate",
                                    "%s cannot be read, so it cannot be copied."
                                    % cycle_id)
            return
        new_id = self._ask("Duplicate cycle", "New id:", cycle_id + "_copy")
        if not new_id:
            return
        document = dict(document, id=new_id)
        if document.get("name"):
            document["name"] += " (copy)"
        self._write_cycle(new_id, document, then_open=True)

    def _rename_cycle(self, cycle_id):
        """Change what a cycle is called - the name on the row, not its id.

        The row shows the name, so renaming from it changes the name. The id is
        the filename and what ``--cycle-run`` is given; changing that is a
        different and rarer thing, and it has its own entry.
        """
        try:
            payload = self.core.cycle_show(cycle_id)
        except core_mod.CoreError as exc:
            widgets.warn(self, "Rename", str(exc))
            return
        if not self._editable(payload, "Rename"):
            return

        document = dict(payload.get("document") or {})
        name = self._ask("Rename cycle", "Name:",
                         document.get("name") or cycle_id)
        if not name or name == document.get("name"):
            return
        document["name"] = name
        self._write_cycle(cycle_id, document, then_open=True)

    def _change_cycle_id(self, cycle_id):
        """Change the id, which renames the file.

        Written under the new id *before* the old one is removed: if the write
        fails there is still a cycle. Anything that named the old id - a shell
        script, a note, somebody's memory - stops finding it, which is why this
        is not what Rename does.
        """
        try:
            payload = self.core.cycle_show(cycle_id)
        except core_mod.CoreError as exc:
            widgets.warn(self, "Change id", str(exc))
            return
        if not self._editable(payload, "Change id"):
            return

        new_id = self._ask("Change id", "Id (letters, digits, _ and -):",
                           cycle_id)
        if not new_id or new_id == cycle_id:
            return
        if any(row.get("id") == new_id for row in self._cycles):
            widgets.note(self, "Change id",
                                    "There is already a cycle with the id %r."
                                    % new_id)
            return
        if not self._confirm(
                "Change id",
                "Rename the file to %s.yaml?\n\nAnything that runs %s by name "
                "- a script, a shortcut - will stop finding it."
                % (new_id, cycle_id)):
            return

        document = dict(payload.get("document") or {}, id=new_id)
        if not self._write_cycle(new_id, document, then_open=True):
            return
        try:
            self.core.cycle_delete(cycle_id)
        except core_mod.CoreError as exc:
            widgets.warn(self, "Change id",
                                "Written as %s, but %s could not be removed: %s"
                                % (new_id, cycle_id, exc))
        self.saved.emit()

    def _editable(self, payload, title):
        """True when this cycle may be written to. Says why when it may not."""
        if not payload.get("document"):
            widgets.note(self, title,
                                    "This cycle cannot be read, so it cannot "
                                    "be changed.")
            return False
        if not payload.get("writable"):
            widgets.note(self, title,
                                    "%s ships with the application. Duplicate "
                                    "it instead." % payload.get("id", ""))
            return False
        return True

    def _delete_cycle(self, cycle_id):
        if not self._confirm("Delete cycle",
                             "Delete %r? The file goes; runs it has already "
                             "made are kept." % cycle_id):
            return
        try:
            answer = self.core.cycle_delete(cycle_id)
        except core_mod.CoreError as exc:
            widgets.warn(self, "Delete", str(exc))
            return
        if not answer.get("ok"):
            widgets.note(self, "Delete",
                                    "\n".join(str(one) for one in
                                               answer.get("problems") or []))
            return
        # The file is gone, so nothing would ever read its secrets again. A
        # credential nobody can see and nobody deletes is the one thing this
        # store exists to avoid, so it goes with the cycle.
        self._one_secret(lambda: self.core.cycle_secret_delete(cycle_id),
                         cycle_id)
        if self.current_id() == cycle_id:
            self._close()
        self.saved.emit()

    def _import_cycle(self):
        from PySide6.QtWidgets import QFileDialog

        path, _filter = QFileDialog.getOpenFileName(
            self, "Import a cycle", "", "Cycle files (*.yaml *.yml)")
        if not path:
            return
        try:
            answer = self.core.cycle_import(path)
        except core_mod.CoreError as exc:
            widgets.warn(self, "Import", str(exc))
            return
        if not answer.get("ok"):
            widgets.note(self, "Import",
                                    "\n".join(str(one) for one in
                                               answer.get("problems") or []))
            return
        self.saved.emit()

    def _write_cycle(self, cycle_id, document, then_open=False):
        """Hand a document to the core. Shows what it refused, if it did.

        Nothing is validated here: the core writes nothing that does not hold,
        and a second opinion in the GUI is a second thing to keep in step.
        """
        if self.core is None:
            return False
        try:
            answer = self.core.cycle_save(cycle_id, document)
        except core_mod.CoreError as exc:
            widgets.warn(self, "Save", str(exc))
            return False
        if not answer.get("ok"):
            widgets.note(
                self, "Save", "This was not written:\n\n- %s"
                              % "\n- ".join(str(one) for one in
                                             answer.get("problems") or []))
            return False
        if then_open:
            # A saved listener may refresh the inventory immediately.
            self._pending_open = cycle_id
        self.saved.emit()
        return True

    def _save_properties(self, changes):
        """The properties form: name, description, project, variables."""
        cycle_id = self.current_id()
        if not cycle_id or not self._open:
            return
        document = dict(self._open.get("document") or {})
        if not document:
            return
        for key, value in changes.items():
            if value:
                document[key] = value
            else:
                document.pop(key, None)
        if self._write_cycle(cycle_id, document):
            self.open(cycle_id)

    def _save_yaml(self, text):
        """The YAML tab: the file as somebody typed it."""
        cycle_id = self.current_id()
        if not cycle_id:
            return
        if self._write_cycle(cycle_id, {"yaml": text}):
            self.open(cycle_id)

    def _close(self):
        """Nothing open: what deleting whatever was on screen leaves behind."""
        self._open = None
        self._selected = ""
        self.canvas.set_graph(None)
        self.title.setText("Cycles")
        self.state.setText("")
        self.output.set_problems([])
        self.output.set_yaml("", writable=False)
        self._update_buttons()

    # -- asking -----------------------------------------------------------------
    def _ask(self, title, label, value=""):
        answer, ok = QInputDialog.getText(self, title, label, QLineEdit.Normal,
                                          value)
        return answer.strip() if ok else ""

    def _confirm(self, title, question):
        return widgets.confirm(self, title, question, agree="Yes",
                               refuse="No")

    def _agreed(self, how):
        """Ask before the expensive ones. True to go ahead.

        Not before a single step: that is the cheap, reversible one, and this
        pair of buttons exists precisely so somebody can press it repeatedly
        while working on one part. A prompt there would be a prompt in the way.

        The other two are worth a question because of what a real cycle does
        rather than because of how long it takes - a full run of the
        development cycle spends six agent calls and moves a Jira issue, and
        neither of those is undone by pressing Stop.
        """
        if how == "resume":
            state = self.run_state.cycle if self.run_state else {}
            return widgets.confirm(
                self, "Resume run", "Continue %s?" % (state or {}).get("run_id", ""),
                "Successful results are kept. Failed steps may call paid services "
                "again. Changed completed steps or uncertain outcomes stop the "
                "resume before more work starts.", agree="Resume")
        if how == "only":
            return True
        if how == "from":
            after = _waiting_on(self.canvas.graph(), self._selected)
            return widgets.confirm(
                self, "Run from here",
                "Run %s and the %d step(s) that wait on it?"
                % (self._selected, len(after) - 1),
                "Earlier results come from the displayed run, or the last run "
                "of this cycle if none is displayed.\n\n" + CONSEQUENCE, agree="Run from here")
        if how == "hard":
            return widgets.confirm(
                self, "Hard Run", "Start %s from the beginning?"
                % (_name_of(self._open) or self.current_id()),
                "All steps will be evaluated again in a new run. Paid services "
                "will be called again. Previous runs remain saved.\n\n" + CONSEQUENCE,
                agree="Hard Run")
        return widgets.confirm(
            self, "Run",
            "Run all %d step(s) of %s?"
            % (len(self.canvas.node_ids()),
               _name_of(self._open) or self.current_id()),
            CONSEQUENCE, agree="Run")

    # -- running one ----------------------------------------------------------
    def _run(self, how=""):
        """Start the cycle, or the part of it the selected step names.

        ``how`` is "" or "hard" for a fresh run, "resume" for continuation,
        "only" for the selected step, and "from" for that step and its descendants.
        """
        cycle_id = self.current_id()
        if not cycle_id:
            return
        if how == "resume":
            state = (self.run_state.cycle or {}) if self.run_state else {}
            if not self._resume_available():
                return
        elif how in ("only", "from") and not self._selected:
            return
        if not self._agreed(how):
            return
        problems = (self._open or {}).get("problems") or []
        if problems:
            widgets.note(
                self, "Run", "Fix this first:\n\n- %s"
                             % "\n- ".join(str(one) for one in problems[:6]))
            return
        if how != "resume":
            self.canvas.clear_status()
            self.output.clear_log()
            self.output.show_log()
        target = (state["run_id"] if how == "resume" else self._selected
                  if how in ("only", "from") else "")
        self.run_requested.emit(cycle_id, "" if how == "hard" else how, target)

    def _graph_known(self, graph):
        """A run started. Draw what it is running, if that is not already drawn.

        A run may have been started from elsewhere - History, a rerun - so the
        canvas should show what is actually going rather than what somebody last
        clicked. But when it is the cycle already on screen, which is the usual
        case, **the scene is kept**: rebuilding it would throw away the items a
        run is about to paint status onto, lose the selection, and flicker the
        whole graph at the moment somebody started watching it.
        """
        if not graph:
            return
        if _same_shape(graph, self.canvas.graph()):
            self.canvas.clear_status()
        else:
            self._flush_view()
            self.canvas.set_graph(graph, self._remembered(graph.get("id")),
                                  self._remembered_view(graph.get("id")))
            self._selected = ""
        self._update_buttons()

    # -- where the nodes sit, and where somebody was looking -------------------
    def _drawn_id(self):
        """Which cycle the canvas is showing, which is not always the open one.

        A run started from History or a rerun swaps the graph without changing
        what is open, and writing that graph's positions under the previously
        opened cycle's id would move the boxes of a cycle nobody touched.
        """
        return (self.canvas.graph() or {}).get("id") or self.current_id()

    def _remembered(self, cycle_id):
        """Where this cycle's nodes were last put. Never raises."""
        try:
            return clf.places_for(cycle_id or "")
        except Exception:                 # noqa: BLE001 - cosmetic state only
            return {}

    def _remembered_view(self, cycle_id):
        """Where this cycle was last looked at from, or None. Never raises."""
        try:
            return clf.view_for(cycle_id or "")
        except Exception:                 # noqa: BLE001 - cosmetic state only
            return None

    def _remember_places(self):
        """Write the arrangement down, or forget it when Arrange undid it.

        Guarded because this is the least important thing on the page: a
        read-only data directory should cost somebody the memory of where they
        dragged a box, and nothing else.
        """
        cycle_id = self._drawn_id()
        if not cycle_id:
            return
        arranged = not self.canvas.moved()
        try:
            clf.remember(cycle_id, {} if arranged else self.canvas.places())
            if arranged:
                # Arrange is somebody asking for the computed layout back, and
                # the view they had been reading the old one from is part of
                # what they are undoing.
                clf.remember_view(cycle_id, None)
        except clf.CycleLayoutFileError:
            pass

    def _view_moved(self):
        """The canvas was zoomed or panned. Write it down once it settles.

        Restarted on every change rather than written on each: a wheel sends
        several a second and a hand drag one per pixel, and a file rewritten
        that often to record where somebody is *still* moving to is a cost paid
        for nothing. A second of quiet is somebody having arrived.
        """
        self._forget_view = False
        self._view_timer.start()

    def _view_reset(self):
        """Fit, however it was asked for. Forget where they had been looking.

        Through the same wait, because a splitter being dragged re-fits on
        every pixel of it: the answer is the same each time and writing it a
        hundred times says nothing the last one does not.
        """
        self._forget_view = True
        self._view_timer.start()

    def _remember_view(self):
        """Write where this cycle is being looked at from. Never fatal."""
        self._view_timer.stop()
        cycle_id = self._drawn_id()
        if not cycle_id:
            return
        try:
            clf.remember_view(cycle_id,
                              None if self._forget_view else self.canvas.view())
        except clf.CycleLayoutFileError:
            pass

    def _flush_view(self):
        """Settle a pending view before the canvas becomes another cycle's."""
        if self._view_timer.isActive():
            self._remember_view()

    def _step_changed(self, step_id):
        state = (self.run_state.cycle or {}) if self.run_state else {}
        step = (state.get("steps") or {}).get(step_id)
        if not step:
            return
        # One node, one repaint - this is already cheap enough to do per event.
        self.canvas.set_status(step_id, step.get("status"),
                               step.get("duration_ms"), step.get("attempt"))
        # Whichever step moved. The panel shows the run, not the selection, so
        # a step nobody has clicked is exactly as much a reason to redraw.
        self._schedule_paint()
        self._schedule_buttons()

    # -- coalescing -----------------------------------------------------------
    # A run reports several times a second - a stage, a batch of output, a step
    # ending - and each of those used to repaint the whole output panel on the
    # spot. The same answer arrived at fifty times is forty-nine repaints spent
    # to display nothing new, on the thread that also has to stay answering.
    #
    # So the work is asked for rather than done: a flag, and a trip through the
    # event loop that does it once. The window does exactly this for the rail's
    # badges - see MainWindow._schedule_nav_badges - and for the same reason.
    def _schedule_paint(self):
        if not self._paint_pending:
            self._paint_pending = True
            QTimer.singleShot(0, self._flush_paint)

    def _flush_paint(self):
        self._paint_pending = False
        self._paint_log()

    def _schedule_buttons(self):
        if not self._buttons_pending:
            self._buttons_pending = True
            QTimer.singleShot(0, self._flush_buttons)

    def _flush_buttons(self):
        self._buttons_pending = False
        self._update_buttons()

    def _node_selected(self, step_id):
        """Selecting narrows the output to that step. Opening it is a double-click.

        Nothing selected is the wider view, not an empty one: a cycle runs
        several steps at once, and what the run as a whole is doing is the
        question somebody watching it usually has. Having to click a node to
        see anything at all meant a running cycle showed nothing until you
        guessed which node was talking.
        """
        self._selected = step_id
        self._paint_log()
        self._update_buttons()

    def _paint_log(self):
        """The output panel: this step's lines, or every step's if none is picked.

        The **stages are always the whole run's**, whatever is selected. They
        were narrowed with the output once, and that made the panel empty far
        more often than it made it precise: only an agent step emits stages, so
        clicking any other node blanked a list that was the only readable
        account of what the cycle was doing. Output is the narrow view; stages
        are the story, and each row says which step it belongs to.
        """
        state = (self.run_state.cycle or {}) if self.run_state else {}
        if self._selected:
            step = (state.get("steps") or {}).get(self._selected) or {}
            self.output.set_log((step.get("log") or []) + _account_of(step))
            self.output.set_log_scope(self._selected)
            self.output.set_summary(self._selected, implementation_html(step))
        else:
            # Sliced before it is formatted, not after: the model keeps
            # CYCLE_RUN_LOG_LINES of them and the panel shows VISIBLE_LINES, so
            # formatting the rest was several thousand strings built per event
            # to be thrown away unread.
            self.output.set_log(
                [(stream, "%s | %s" % (step_id, line))
                 for step_id, stream, line
                 in (state.get("log") or [])[-VISIBLE_LINES:]])
            self.output.set_log_scope("")
            self.output.set_summary("", "")
        self.output.set_stages(list(state.get("stages") or []), named=True,
                               labels=self._labels())

    def _labels(self):
        """What each step is called, by id. Empty when nothing is drawn.

        The graph on the canvas rather than the one the run announced: they are
        the same graph in every ordinary case, and when they are not it is the
        one somebody is looking at that its rows should be named after.
        """
        nodes = (self.canvas.graph() or {}).get("nodes") or []
        return {node.get("id"): node.get("label") or node.get("id")
                for node in nodes if node.get("id")}

    def _resume_available(self):
        state = (self.run_state.cycle or {}) if self.run_state else {}
        return bool(self.run_state and not self.run_state.cycle_running
                    and state.get("cycle") == self.current_id()
                    and state.get("run_id")
                    and state.get("status") in
                    ("failed", "cancelled", "interrupted", "timeout", "error"))

    def _update_buttons(self):
        # The cycle's own flag, not the scenario run's: with the two conflated,
        # starting anything on the Run page lit Stop here for a cycle that had
        # finished long before.
        running = bool(self.run_state and self.run_state.cycle_running)
        has_cycle = bool(self.current_id())
        self.run_button.setEnabled(has_cycle and not running)
        self.hard_run_action.setEnabled(has_cycle and not running)
        resume = self._resume_available()
        self.run_button.setText("Resume" if resume else "Run")
        self.run_button.setToolTip(
            "Continue the displayed run using its saved successful results.\n"
            "Failed or unfinished steps may call paid services again."
            if resume else "Start a new run of this cycle.")
        hint = ("Resume keeps successful results and continues unfinished work. "
                "Hard Run in the menu starts from the beginning and calls paid services again."
                if resume else "Run starts a new execution. After a failure or stop, "
                "Resume keeps successful results. Hard Run in the menu starts from the beginning.")
        self.run_hint.setText(hint)
        self.run_hint.setVisible(has_cycle)
        # Only with a step picked: they run the selected one, and offering
        # them with nothing selected would be a button for a thing that has
        # not been decided yet.
        picked = bool(self._selected) and self.canvas.node(self._selected)
        for button in (self.step_button, self.from_button):
            button.setEnabled(bool(picked) and has_cycle and not running)
        self.stop_button.setEnabled(running)
        self.subjects.set_busy(running)
        drawn = bool(self.canvas.node_ids())
        self.properties_button.setEnabled(bool(self.current_id()))
        for button in (self.fit_button, self.zoom_in_button,
                       self.zoom_out_button):
            button.setEnabled(drawn)
        # Offered only once there is something to put back, so it reads as the
        # answer to a canvas somebody has rearranged rather than as a button
        # that does nothing.
        self.arrange_button.setEnabled(drawn and self.canvas.moved())


def _framed(canvas):
    """The canvas in a panel, so it sits in the page like every other surface."""
    holder = QWidget()
    holder.setProperty("role", "panel")
    layout = QVBoxLayout(holder)
    layout.setContentsMargins(1, 1, 1, 1)
    layout.addWidget(canvas)
    return holder


def _name_of(payload):
    graph = payload.get("cycle") or {}
    return graph.get("name") or payload.get("id") or ""


#: How much of one output is shown before it is cut. A step can carry a whole
#: Jira issue in one; the panel is for "what happened", and the record on disk
#: is for the rest.
OUTPUT_CHARS = 300


def _account_of(step):
    """What a finished step came to, as log lines. ``[]`` while it runs.

    The panel used to show only what a step *printed*, which for half of them
    is nothing at all - a Jira step returns issues, a memory step returns what
    it remembered, and neither says a word on its way past. So a run could
    finish, having done exactly what was asked, and leave the one place
    somebody looks blank.
    """
    status = step.get("status") or ""
    if status in ("", "pending", "running", "waiting"):
        return []

    said = [("out", ""), ("out", "-- %s" % status)]
    if step.get("message"):
        said.append(("out", step["message"]))
    for name in sorted(step.get("outputs") or {}):
        said.append(("out", "   %s = %s" % (name,
                                            _one_line(step["outputs"][name]))))
    for one in step.get("artifacts") or []:
        where = one.get("path") if isinstance(one, dict) else one
        if where:
            said.append(("out", "   file: %s" % where))
    return said


def _one_line(value):
    """One output, on one line, cut where it stops being readable.

    Not ``_short`` - this file already has one, for the Inspector's much
    narrower column, and the later definition was quietly winning.
    """
    text = " ".join(str(value).split())
    return text if len(text) <= OUTPUT_CHARS else text[:OUTPUT_CHARS] + "..."


def _waiting_on(graph, step_id):
    """``step_id`` and every step that could only run after it.

    Worked out here rather than asked of the core, because it is wanted to
    write a sentence in a dialog and a round trip to a subprocess for that
    would be absurd. The core has its own, which is what actually selects the
    steps - this one only has to count them.
    """
    nodes = (graph or {}).get("nodes") or []
    found = {step_id} if any(one["id"] == step_id for one in nodes) else set()
    changed = True
    while changed:
        changed = False
        for node in nodes:
            if node["id"] in found:
                continue
            if any(need in found for need in node.get("needs") or ()):
                found.add(node["id"])
                changed = True
    return found


def _same_shape(one, other):
    """True when two graph payloads would draw the same picture.

    Compared on what is actually drawn - which nodes, where, and what joins
    them - rather than with ``==``: a run's payload may carry a description or
    a variable that differs from the file on disk without a single node moving,
    and rebuilding the scene for that would be a flicker for nothing.
    """
    if not one or not other:
        return False

    def shape(graph):
        return ([(node.get("id"), node.get("layer"), node.get("row"),
                  node.get("plugin"), node.get("label"))
                 for node in graph.get("nodes") or []],
                [(edge.get("from"), edge.get("to"), edge.get("kind"))
                 for edge in graph.get("edges") or []])

    return shape(one) == shape(other)


class _Explorer(QWidget):
    """The projects, and the cycles in them.

    A tree from the start rather than a list, because there will be more in it:
    Runs and Artifacts belong here too eventually, and a list would have to
    become a tree later and every call site with it.

    It does not own anything - the page holds the projects and the cycles, and
    every action here is a signal. That keeps the one thing that writes files in
    one place, which is what stops two halves of a page disagreeing about what
    is on disk.
    """

    opened = Signal(str)                 # a cycle id
    selection_changed = Signal()
    project_added = Signal()
    project_renamed = Signal(str)        # the old name
    project_deleted = Signal(str)
    cycle_added = Signal(str)            # the project to put it in
    cycle_duplicated = Signal(str)
    cycle_renamed = Signal(str)          # change what it is called
    cycle_reidentified = Signal(str)     # change the id, which renames the file
    cycle_deleted = Signal(str)
    cycle_imported = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = {}                  # cycle id -> item
        self._projects = {}              # project name -> item
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 12, 0)
        layout.setSpacing(8)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search cycles")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.itemActivated.connect(self._activated)
        self.tree.itemClicked.connect(self._activated)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.setProperty("role", "panel")
        layout.addWidget(self.tree, 1)

        # No button row: everything here is a right-click away, which is where
        # a tree's actions belong. The menu is built per row, so it offers what
        # can actually be done to the thing under the pointer - and on empty
        # ground it still offers the two that need nothing selected.
        self.tree.currentItemChanged.connect(lambda *_a: self.selection_changed.emit())

        self.count = widgets.mono("")
        layout.addWidget(self.count)

    # -- filling it -----------------------------------------------------------
    def fill(self, cycles, projects):
        """Rebuild from the cycles and the projects, keeping what was selected.

        Both kinds of selection, not only a cycle. The tree is rebuilt whenever
        anything is written - including writing the projects file itself - so a
        project that lost its selection every time meant Rename and Delete went
        grey the instant you made the project you wanted to rename.
        """
        was_cycle, was_project = self.current_id(), self._selected_project()
        self.tree.clear()
        self._rows, self._projects = {}, {}

        known = [project.name for project in projects if project.name]
        grouped = {name: [] for name in known}
        for row in cycles:
            name = row.get("project") or ""
            grouped.setdefault(name if name in grouped else UNASSIGNED,
                               []).append(row)

        for name in known:
            self._group(name, grouped.get(name) or [], real=True)
        # Only when there is something in it: an empty Unassigned group is a
        # question nobody asked.
        if grouped.get(UNASSIGNED):
            self._group(UNASSIGNED, grouped[UNASSIGNED], real=False)

        self.count.setText("%d cycle%s in %d project%s"
                           % (len(cycles), "" if len(cycles) == 1 else "s",
                              len(known), "" if len(known) == 1 else "s"))
        if was_cycle:
            self.select(was_cycle)
        elif was_project:
            self.select_project(was_project)
        self._filter(self.search.text())

    def _group(self, name, rows, real):
        item = QTreeWidgetItem(self.tree, [name])
        item.setData(0, ROLE_KIND, "project" if real else "unassigned")
        item.setData(0, ROLE_ID, name if real else "")
        item.setExpanded(True)
        item.setIcon(0, icons.icon("group", theme.NEUTRAL[600]))
        item.setToolTip(0, "%d cycle(s)" % len(rows))
        self._projects[name] = item

        if not rows:
            empty = QTreeWidgetItem(item, ["No cycles yet"])
            empty.setData(0, ROLE_KIND, "empty")
            empty.setFlags(Qt.ItemIsEnabled)
            return

        for row in sorted(rows, key=lambda one: (one.get("name")
                                                 or one.get("id", "")).lower()):
            self._leaf(item, row)

    def _leaf(self, parent, row):
        item = QTreeWidgetItem(parent, [row.get("name") or row.get("id", "")])
        item.setData(0, ROLE_KIND, "cycle")
        item.setData(0, ROLE_ID, row.get("id", ""))
        if row.get("problems"):
            item.setIcon(0, icons.icon("fail", theme.BAD))
            item.setToolTip(0, "\n".join(str(one)
                                          for one in row["problems"][:6]))
        else:
            item.setIcon(0, icons.icon("cycles", theme.NEUTRAL[600]))
            item.setToolTip(0, row.get("description") or row.get("id", ""))
        if not row.get("writable"):
            item.setForeground(0, theme.color(theme.NEUTRAL[500]))
        self._rows[row.get("id", "")] = item

    # -- what is selected -----------------------------------------------------
    def select(self, cycle_id):
        item = self._rows.get(cycle_id)
        if item is not None and item is not self.tree.currentItem():
            self.tree.setCurrentItem(item)

    def select_project(self, name):
        item = self._projects.get(name)
        if item is not None and item is not self.tree.currentItem():
            self.tree.setCurrentItem(item)

    def _selected_project(self):
        """The project row that is selected, if it is a project row itself.

        Not :meth:`current_project`, which walks up from a cycle to the project
        holding it - restoring that would move the selection off the cycle and
        onto its group.
        """
        item = self.tree.currentItem()
        if item is not None and item.data(0, ROLE_KIND) == "project":
            return item.data(0, ROLE_ID) or ""
        return ""

    def current_id(self):
        item = self.tree.currentItem()
        if item is not None and item.data(0, ROLE_KIND) == "cycle":
            return item.data(0, ROLE_ID) or ""
        return ""

    def current_project(self):
        """Which project a New Cycle should go into, from what is selected."""
        item = self.tree.currentItem()
        while item is not None:
            if item.data(0, ROLE_KIND) == "project":
                return item.data(0, ROLE_ID) or ""
            item = item.parent()
        return ""

    def current_kind(self):
        item = self.tree.currentItem()
        return item.data(0, ROLE_KIND) if item is not None else ""

    def set_developer_mode(self, enabled):
        pass          # nothing here is developer-only yet

    # -- acting on it ---------------------------------------------------------
    def _activated(self, item, _column=0):
        if item is not None and item.data(0, ROLE_KIND) == "cycle":
            self.opened.emit(item.data(0, ROLE_ID) or "")

    def _menu(self, point):
        item = self.tree.itemAt(point)
        kind = item.data(0, ROLE_KIND) if item is not None else ""
        menu = QMenu(self)

        if kind == "cycle":
            cycle_id = item.data(0, ROLE_ID)
            menu.addAction("Open", lambda: self.opened.emit(cycle_id))
            menu.addSeparator()
            menu.addAction("Rename cycle...",
                           lambda: self.cycle_renamed.emit(cycle_id))
            # Its own entry, and worded as what it is. The id is the filename
            # and what --cycle-run is given; renaming is changing the label on
            # the row you just right-clicked. Offering one as the other is how
            # somebody ends up with a cycle called "Nightly validation" whose
            # id is "nightly_validation_2".
            menu.addAction("Change id (%s)..." % cycle_id,
                           lambda: self.cycle_reidentified.emit(cycle_id))
            menu.addAction("Duplicate...",
                           lambda: self.cycle_duplicated.emit(cycle_id))
            menu.addAction("Delete...",
                           lambda: self.cycle_deleted.emit(cycle_id))
            menu.addSeparator()
        elif kind == "project":
            name = item.data(0, ROLE_ID)
            menu.addAction("New cycle here...",
                           lambda: self.cycle_added.emit(name))
            menu.addSeparator()
            menu.addAction("Rename project...",
                           lambda: self.project_renamed.emit(name))
            menu.addAction("Delete project...",
                           lambda: self.project_deleted.emit(name))
            menu.addSeparator()

        menu.addAction("New project...", self.project_added.emit)
        menu.addAction("New cycle...",
                       lambda: self.cycle_added.emit(self.current_project()))
        menu.addAction("Import a cycle file...", self.cycle_imported.emit)
        menu.exec(self.tree.viewport().mapToGlobal(point))

    def _filter(self, text):
        needle = (text or "").strip().lower()
        for cycle_id, item in self._rows.items():
            haystack = "%s %s" % (cycle_id, item.text(0))
            item.setHidden(bool(needle) and needle not in haystack.lower())
        # A project whose every cycle is filtered out goes with them, so the
        # search reads as a shorter tree rather than as a list of empty groups.
        for item in self._projects.values():
            shown = any(not item.child(index).isHidden()
                        for index in range(item.childCount()))
            item.setHidden(bool(needle) and not shown)


class _Output(QWidget):
    """What a step printed, anything wrong with the cycle, and the file itself.

    The YAML tab is editable because the grammar has corners a form should not
    grow a widget for - conditions, ``needs:``, a step's whole ``with:`` - and
    hiding them would make the editor a worse tool than the text editor it
    replaces. That is the same bargain the Scenarios page already strikes.
    """

    save_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._original = ""
        self._painted = []               # the output lines already on screen
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)

        self.tabs = QTabWidget()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setStyleSheet("font-family: %s; font-size: 11px;"
                               % theme.MONO_CSS)
        self.summary = QTextBrowser()
        self.summary.setOpenLinks(False)
        self.summary.setOpenExternalLinks(False)
        self.summary.document().setDocumentMargin(14)
        self.summary.setStyleSheet("font-size: 13px;")
        self._summary_scope, self._summary_html = "", ""
        self.output_views = QStackedWidget()
        self.output_views.addWidget(self.log)
        self.output_views.addWidget(self.summary)
        self.details_button = QPushButton("Technical details")
        self.details_button.setCheckable(True)
        self.details_button.setToolTip("Show the original log, outputs and artifact paths.")
        self.details_button.toggled.connect(self._toggle_details)
        self.details_button.hide()
        output = QWidget()
        output_layout = QVBoxLayout(output)
        output_layout.setContentsMargins(0, 0, 0, 0)
        output_layout.setSpacing(0)
        output_layout.addWidget(widgets.row(None, self.details_button))
        output_layout.addWidget(self.output_views, 1)
        self.problems = QPlainTextEdit()
        self.problems.setReadOnly(True)
        self.problems.setStyleSheet("font-family: %s; font-size: 11px;"
                                    % theme.MONO_CSS)
        self.yaml = QPlainTextEdit()
        self.yaml.setReadOnly(True)
        self.yaml.setStyleSheet("font-family: %s; font-size: 11px;"
                                % theme.MONO_CSS)
        self.stages = StageList()
        self.tabs.addTab(output, "Output")
        self.tabs.addTab(self.stages, "Stages")
        self.tabs.addTab(self.problems, "Problems")

        editor = QWidget()
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(6)
        editor_layout.addWidget(self.yaml, 1)
        self.revert_button = QPushButton("Revert")
        self.save_button = QPushButton("Save")
        self.save_button.setProperty("variant", "primary")
        self.revert_button.clicked.connect(self._revert)
        self.save_button.clicked.connect(
            lambda: self.save_requested.emit(self.yaml.toPlainText()))
        self.yaml.textChanged.connect(self._changed)
        editor_layout.addWidget(widgets.row(None, self.revert_button,
                                            self.save_button))
        self.tabs.addTab(editor, "YAML")
        layout.addWidget(self.tabs)
        self._changed()

    def set_log(self, lines):
        """Repaint the output, following the newest line unless somebody moved.

        The whole text is replaced on every batch, so "stay where I was" has to
        be decided before it goes: a reader who scrolled up to look at a step's
        failure was otherwise thrown to the bottom every time another line
        arrived, which during a run is several times a second.

        Text the panel is already showing is not painted again. The page
        redraws whenever anything about the run moves - a stage arriving is
        reason enough - and most of those say nothing new about the output.
        Deliberately not an append-only view: log lines repeat, so working out
        what is new by comparing text would eventually get it wrong, and a
        mangled log is worse than a redrawn one.
        """
        painted = [line for _stream, line in lines[-VISIBLE_LINES:]]
        if painted == self._painted:
            return
        bar = self.log.verticalScrollBar()
        following = bar.value() >= bar.maximum() - LOG_SLACK
        where = bar.value()
        self.log.setPlainText("\n".join(painted))
        self._painted = painted
        bar.setValue(bar.maximum() if following else min(where, bar.maximum()))

    def clear_log(self):
        self.log.setPlainText("")
        self._painted = []
        self.set_summary("", "")
        self.set_stages([])

    def set_summary(self, step_id, document):
        if step_id != self._summary_scope:
            self.details_button.setChecked(False)
            self._summary_scope = step_id
        self.details_button.setVisible(bool(document))
        if document != self._summary_html:
            bar = self.summary.verticalScrollBar()
            position = bar.value() if self._summary_html else 0
            self.summary.setHtml(document)
            bar.setValue(position)
            self._summary_html = document
        self._toggle_details(self.details_button.isChecked())

    def _toggle_details(self, checked):
        self.output_views.setCurrentWidget(
            self.log if checked or not self._summary_html else self.summary)
        self.details_button.setText("Readable summary" if checked else "Technical details")

    def set_stages(self, stages, named=False, labels=None):
        """The stage rows, and how many there are in the tab's name."""
        self.stages.set_stages(stages, named=named, labels=labels)
        self.tabs.setTabText(TAB_STAGES, "Stages (%d)" % len(stages) if stages
                             else "Stages")

    def set_log_scope(self, step_id):
        """Say in the tab name whose output is on show.

        "Output" alone is ambiguous once the panel has two scopes - a reader
        seeing one step's lines has no way to tell that from the run being
        quiet.
        """
        self.tabs.setTabText(TAB_OUTPUT,
                             "Output - %s" % step_id if step_id else "Output")

    def show_log(self):
        self.tabs.setCurrentIndex(TAB_OUTPUT)

    def set_problems(self, problems):
        self.problems.setPlainText("\n".join(str(one) for one in problems))
        self.tabs.setTabText(TAB_PROBLEMS, "Problems (%d)" % len(problems)
                             if problems else "Problems")

    def set_yaml(self, text, writable=True):
        self._original = text or ""
        self.yaml.blockSignals(True)
        self.yaml.setPlainText(self._original)
        self.yaml.blockSignals(False)
        self.yaml.setReadOnly(not writable)
        self._writable = bool(writable)
        self._changed()

    def _revert(self):
        self.yaml.setPlainText(self._original)

    def _changed(self):
        """Save and Revert are offered only when there is something to undo."""
        dirty = (self.yaml.toPlainText() != self._original
                 and getattr(self, "_writable", False))
        self.save_button.setEnabled(dirty)
        self.revert_button.setEnabled(dirty)
        self.tabs.setTabText(TAB_YAML, "YAML *" if dirty else "YAML")


def _status_colour(status):
    _mark, name = STATUS_LOOK.get(status, STATUS_LOOK["pending"])
    if name.startswith("NEUTRAL"):
        return theme.NEUTRAL[int(name[len("NEUTRAL"):])]
    return getattr(theme, name)


def _duration(milliseconds):
    seconds = (milliseconds or 0) / 1000.0
    if seconds < 1:
        return "%dms" % (milliseconds or 0)
    if seconds < 60:
        return "%.1fs" % seconds
    minutes, seconds = divmod(int(seconds), 60)
    return "%dm %02ds" % (minutes, seconds)


def _short(value):
    text = str(value)
    return text if len(text) <= 60 else text[:60] + "..."


def _clean(number):
    try:
        value = float(number)
    except (TypeError, ValueError):
        return number
    return int(value) if value.is_integer() else value


def _with_listening(document, old_id, new_id, listening):
    """``document`` with the trigger watching one step brought in line with
    what its dialog said: renamed with the step, turned on or off, rescheduled.

    Off is written as ``enabled: false`` rather than the trigger being deleted,
    so turning it back on keeps its schedule. A step that was never listened to
    and still is not gets no trigger at all.
    """
    triggers = [dict(one) for one in document.get("triggers") or []
                if isinstance(one, dict)]
    mine = None
    for one in triggers:
        if one.get("watch") == old_id:
            one["watch"] = new_id
            mine = one
    if listening is not None:
        if mine is None and listening.get("enabled"):
            taken = {one.get("id") for one in triggers}
            name = "listen_" + new_id
            mine = {"id": name if name not in taken else name + "_2", "watch": new_id}
            triggers.append(mine)
        if mine is not None:
            mine["every"] = listening["every"]
            if listening["enabled"]:
                mine.pop("enabled", None)
            else:
                mine["enabled"] = False
    changed = dict(document)
    if triggers:
        changed["triggers"] = triggers
    else:
        changed.pop("triggers", None)
    return changed
