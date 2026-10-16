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

from PySide6.QtCore import QEvent, QObject, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (QApplication, QDialog, QFrame, QHBoxLayout,
                               QLabel, QMenu,
                               QPlainTextEdit, QPushButton, QStackedWidget,
                               QTabWidget, QTextBrowser, QToolButton,
                               QVBoxLayout, QWidget, QWidgetAction)

from .. import (core as core_mod, cyclelayoutfile as clf,
                cyclepresetsfile as presets_file, cycleprojectsfile as cpf,
                icons, theme, widgets)
from .. import background, findbar, search, stageswindow
from ..cycledraft import Draft
from ..cyclesubjects import KEEP
# The explorer lives in its own module; its roles are re-exported here, where
# the page's callers and tests have always found them.
from ..cycleexplorer import (ROLE_ALERT, ROLE_ID, ROLE_KIND, ROLE_META,  # noqa: F401
                             ROLE_ROW, UNASSIGNED, Explorer as _Explorer)
from ..cyclegraph import STATUS_LOOK, CanvasSkeleton, CycleCanvas
from ..cycleoutput import implementation_html
from ..cyclesubjects import PAGE as SUBJECTS_PAGE, SPEC as SUBJECTS_SPEC, SubjectsPanel
from ..stages import StageList
from ..cycleinteractions import InteractionComposer
from .cycledialogs import CycleDialog, NodeDialog


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

#: Where the explorer's open and shut projects are kept - ``Settings.folds``,
#: the store Services & Logs keeps its own folds in.
FOLDS = "cycles"

#: The output panel's tabs, by position. Named because three places set a tab's
#: text by index and a fourth tab inserted in the middle would otherwise rename
#: the wrong one - which is exactly what happened when Stages was added.
TAB_OUTPUT, TAB_STAGES, TAB_PROBLEMS, TAB_YAML = range(4)

#: Lines of a step's output painted at once. The model keeps more (see
#: cms_gui.runner.CYCLE_LOG_LINES); this is only what is worth re-rendering.
VISIBLE_LINES = 400

#: How far off the bottom of the output still counts as being at it.
LOG_SLACK = 2

#: The two things the canvas can be showing - see CyclesPage.context_switch.
DESIGN, RUN = "Design", "Run"

#: The action that starts a cycle again from nothing, whatever the last run did.
START_OVER = "Start over (new run)"


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
    #: A planned entry double-clicked - start it now or later.
    planned_opened = Signal(dict)
    #: The plan as just read: what the scheduler starts on time.
    planned_loaded = Signal(list)
    stop_requested = Signal()
    interaction_requested = Signal(dict)
    saved = Signal()
    #: The page put the rows a write answered with into its own list - every
    #: row, as it is now - so the window can follow without reading the whole
    #: inventory again. ``saved`` is still said by a core too old to answer so.
    cycles_changed = Signal(list)
    #: Show this on the Artifacts page: (run directory, path relative to it -
    #: "" for the directory itself).
    artifacts_requested = Signal(str, str)
    #: Stages went into a window of its own (True) or came back (False).
    stages_detached_changed = Signal(bool)

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
        self._written = {}               # the last successful --cycle-save answer
        self._selected = ""              # step id selected on the canvas
        # Repaints asked for but not yet done - see _schedule_paint.
        self._paint_pending = False
        self._buttons_pending = False
        self._sessions_pending = False
        self._sessions_loaded = False
        #: The cycle the loaded sessions were asked to put first.
        self._sessions_first = ""
        self._stages_window = None       # stageswindow.StagesWindow, while detached
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
        # Named for what it does. "Hard Run" said it was forceful without
        # saying what it forced; what it does is start again from nothing.
        self.hard_run_action = self.run_menu.addAction(START_OVER)
        self.hard_run_action.setToolTip(
            "A new run from the first step, taking nothing from earlier runs.\n"
            "Paid services will be called again; previous runs remain saved.")
        self.hard_run_action.triggered.connect(lambda: self._run("hard"))
        self.run_button.setMenu(self.run_menu)
        # No text on these two: they act on whichever step is selected, and a
        # label would have to name it or be vague. The tooltip says both what
        # it does and what it borrows, because "run one step" raises the
        # question of where the others' results come from.
        self.step_button = icons.button(QPushButton(), "run-step")
        self.step_button.setToolTip(
            "Run this step only - a new run that takes every other step's\n"
            "results from the run on screen, or the last run if none is.")
        self.step_button.clicked.connect(lambda: self._run("only"))
        self.from_button = icons.button(QPushButton(), "run-from")
        self.from_button.setToolTip(
            "Run from here - this step and everything after it, in a new run\n"
            "that takes the steps before it from the run on screen, or the\n"
            "last run if none is.")
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
        self.fit_button = icons.button(QPushButton(), "fit", "Fit")
        self.fit_button.setToolTip("Show all of it.")
        self.zoom_out_button = icons.button(QPushButton(), "minus")
        self.zoom_out_button.setToolTip("Zoom out (Ctrl+wheel).")
        self.zoom_in_button = icons.button(QPushButton(), "plus")
        self.zoom_in_button.setToolTip("Zoom in (Ctrl+wheel).")
        # Folding is how a long cycle is read: twenty-eight steps become the
        # handful of phases they are grouped into, each one box that says how
        # it went. Offered only when the cycle has phases to fold.
        self.fold_button = icons.button(QPushButton(), "fold", "Fold phases")
        self.fold_button.setToolTip(
            "Show each phase as one box. Double-click a folded phase, or click "
            "a phase's heading, to open or fold just that one.")
        self.fold_button.clicked.connect(self._toggle_folding)
        # What the core thinks of the file, where the file is drawn - and a
        # way to the list of what is wrong with it.
        self.validity = QPushButton("")
        self.validity.setToolTip("Show the Problems tab.")
        self.validity.clicked.connect(
            lambda: self.output.tabs.setCurrentIndex(TAB_PROBLEMS))
        # Which graph the canvas is: the one being written (Design) or the one
        # a run is going through (Run). They are the same picture until the
        # file changes under a run, or a change is not saved yet - and then
        # painting one's colours on the other would be a lie about both.
        self.context_switch = widgets.Segmented([DESIGN, RUN], DESIGN)
        self.context_switch.changed.connect(self._context_changed)
        self._context = DESIGN
        self.add_button = icons.button(QPushButton(), "plus", "Step")
        self.add_button.setToolTip(
            "Add a step - after the selected one, when one is selected.\n"
            "Right-click a step for what else can be done to it.")
        self.add_button.clicked.connect(self._add_step_from_bar)
        for button in (self.properties_button, self.arrange_button,
                       self.fit_button, self.zoom_out_button,
                       self.zoom_in_button, self.fold_button, self.validity,
                       self.add_button):
            button.setProperty("variant", "ghost")

        # What a change made on the canvas and not saved yet looks like, and
        # the ways out of it. Hidden while there is nothing unsaved.
        self._draft = None
        self.draft_note = widgets.mono("")
        self.undo_button = icons.button(QPushButton(), "undo", "Undo")
        self.undo_button.setToolTip("Undo the last change (Ctrl+Z).")
        self.undo_button.clicked.connect(self._undo)
        self.redo_button = icons.button(QPushButton(), "redo", "Redo")
        self.redo_button.setToolTip("Redo it (Ctrl+Shift+Z).")
        self.redo_button.clicked.connect(self._redo)
        self.changes_button = QPushButton("Changes")
        self.changes_button.setToolTip("What saving would change in the file.")
        self.changes_button.clicked.connect(self._show_changes)
        self.revert_draft_button = QPushButton("Revert")
        self.revert_draft_button.setToolTip("Throw the unsaved changes away.")
        self.revert_draft_button.clicked.connect(self._revert_draft)
        self.save_draft_button = QPushButton("Save")
        self.save_draft_button.setProperty("variant", "primary")
        self.save_draft_button.setToolTip("Write the changes to the cycle file (Ctrl+S).")
        self.save_draft_button.clicked.connect(self._save_draft)
        for button in (self.undo_button, self.redo_button, self.changes_button,
                       self.revert_draft_button):
            button.setProperty("variant", "ghost")
        self.draft_bar = _FloatingBar(self.draft_note, None, self.undo_button,
                                      self.redo_button, self.changes_button,
                                      self.revert_draft_button,
                                      self.save_draft_button)
        self.draft_bar.setVisible(False)
        for keys, slot in ((QKeySequence.Undo, self._undo),
                           (QKeySequence.Redo, self._redo),
                           (QKeySequence.Save, self._save_shortcut)):
            shortcut = QShortcut(keys, self)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)
        # Not ghost, deliberately. Ghost means a transparent border, which is
        # right for the view controls beside them and wrong for these: they
        # start work, the way Run and Stop do, and a button that starts
        # something should look like a button.

        self.title = widgets.elided_line("Cycles")
        self.state = widgets.elided_line("", "mono")

        layout.addWidget(widgets.row(
            self.title, None, self.state, self.properties_button,
            self.step_button, self.from_button,
            self.stop_button, self.run_button))
        # Said while nothing is open, on the empty canvas - not as lines over
        # it, which took room from a long cycle and moved the page when they
        # went away.
        self.intro = widgets.lede(
            "A cycle runs services, scenarios, commands and reports as one "
            "graph. Steps that do not depend on each other run at the same "
            "time. Pick one on the left to see all of it - drag the canvas to "
            "move around it, drag a step to shift it, and Arrange puts them "
            "all back. Double-click a step to open it.")

        self.canvas = CycleCanvas(self)
        # Over the canvas rather than in the page's column: the help while
        # nothing is open, and the bar for unsaved changes while there are
        # some. Neither changes how tall anything is when it comes or goes.
        self.intro.setParent(self.canvas.viewport())
        self.intro.setAlignment(Qt.AlignCenter)
        _Pinned(self.intro, self.canvas.viewport(), _centred)
        self.draft_bar.setParent(self.canvas.viewport())
        _Pinned(self.draft_bar, self.canvas.viewport(), _along_the_foot)
        self.canvas.node_selected.connect(self._node_selected)
        self.canvas.node_opened.connect(self.open_step)
        self.canvas.node_menu.connect(self._node_menu)
        self.canvas.canvas_menu.connect(self._canvas_menu)
        self.canvas.edge_menu.connect(self._edge_menu)
        self.canvas.link_made.connect(self._link_made)
        self.canvas.link_moved.connect(self._link_moved)
        self.canvas.link_removed.connect(self._remove_link)
        self.canvas.nodes_moved.connect(self._remember_places)
        self.canvas.view_changed.connect(self._view_moved)
        self.canvas.view_reset.connect(self._view_reset)
        self.canvas.folds_changed.connect(self._folds_changed)
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
        self.explorer.cycle_moved.connect(self._move_cycle)
        self.explorer.project_listed.connect(self._list_project)
        if self.settings is not None:
            self.explorer.set_folds(self.settings.folds(FOLDS))
            self.explorer.folds_changed.connect(self._remember_folds)
        self.output = _Output(self)
        self.output.composer.submitted.connect(self.interaction_requested.emit)
        self.output.save_requested.connect(self._save_yaml)
        self.output.detach_requested.connect(self.detach_stages)
        self.output.dock_requested.connect(self.dock_stages)
        self.output.show_window_requested.connect(self.show_stages)
        self.output.artifact_picked.connect(self._reveal)
        self.subjects = SubjectsPanel(self)
        self.subjects.activated.connect(self._open_session)
        self.subjects.run_picked.connect(self._open_session_run)
        self.subjects.new_requested.connect(lambda: self._run(""))
        self.subjects.delete_requested.connect(self._delete_session)
        self.subjects.planned_start.connect(self.planned_started.emit)
        self.subjects.planned_open.connect(self.planned_opened.emit)
        # One Ctrl+F for the page, sent to the reader the person is in.
        findbar.install(self, self._find_in)
        self.subjects.planned_remove.connect(self._remove_planned)
        self.subjects.files_picked.connect(self._open_run_files)
        self.subjects.more_requested.connect(self._more_sessions)

        # Folding splitters: any panel dragged shut leaves a bold line where
        # it went, and clicking the line brings it back.
        # The view's own controls sit on the view, not in the page's header:
        # they act on what is drawn, and the header is for the cycle itself
        # and for running it - which is also what lets its name be read.
        self.view_bar = widgets.row(
            self.context_switch, self.validity, None, self.add_button,
            self.fold_button, self.arrange_button,
            self.zoom_out_button, self.zoom_in_button, self.fit_button,
            spacing=2)
        across = widgets.FoldingSplitter(Qt.Horizontal)
        across.addWidget(self.explorer)
        across.addWidget(_framed(self.canvas, self.view_bar))
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
        # Kept to open the output again when somebody goes to Stages with it
        # folded shut - see show_stages.
        self._down = down

        # Where a region is waiting for its data - see open and
        # refresh_sessions. Each sits over its own region only.
        self._asked = {}
        tabs = self.output.tabs
        self.skeletons = {
            # The canvas's own paper and node sizes, not a generic grey graph.
            "canvas": CanvasSkeleton(self.canvas),
            "output": widgets.Skeleton(tabs, "lines", area=lambda: tabs.rect().adjusted(
                0, tabs.tabBar().height(), 0, 0)),
            "subjects": widgets.Skeleton(self.subjects.list.viewport(), "rows"),
            # And the pane under the list, which would otherwise say "Nothing
            # has run yet" about runs it has not been told of yet.
            "subjects_detail": widgets.Skeleton(self.subjects.details.viewport(),
                                                "lines"),
            "explorer": widgets.Skeleton(self.explorer.tree.viewport(), "rows"),
        }
        # The page held still, and the canvas veiled with what is happening,
        # while a change is checked or a cycle written - see _waiting.
        self.busy = widgets.Busy(self, area=self.canvas)

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
        elif self._open and self._changed_since_opened():
            # Keep whatever was open across a refresh, so saving a scenario
            # elsewhere does not close the cycle somebody was reading.
            self.open(self._open.get("id", ""))
        self._update_buttons()

    def _changed_since_opened(self):
        """Whether the open cycle's file says something new, by its digest.

        Every save refreshes the inventory, and the page used to read the open
        cycle again each time - a second trip to the core, on this thread,
        for the file it had read a moment before. The inventory carries each
        file's digest, so an unchanged file is not read twice. No digest (an
        older core) means "ask", as it always did.
        """
        cycle_id = (self._open or {}).get("id", "")
        row = next((one for one in self._cycles if one.get("id") == cycle_id), None)
        if row is None:
            return True
        return not row.get("digest") or row.get("digest") != self._open.get("digest")

    def set_run_state(self, run_state):
        """Follow a live run. Connected once; the window owns the model."""
        self.run_state = run_state
        run_state.cycle_graph_known.connect(self._graph_known)
        run_state.cycle_step_changed.connect(self._step_changed)
        run_state.cycle_interactions_changed.connect(self._schedule_paint)
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
            self.explorer.set_error("")
        except cpf.CycleProjectsFileError as exc:
            self._projects = []
            self.output.set_problems([str(exc)])
            # Beside the tree as well, because a tree of cycles with every
            # project gone looks like the projects were lost, not unreadable.
            self.explorer.set_error("The projects list could not be read, so "
                                    "cycles are grouped by what their files say. "
                                    "%s" % exc)

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
    def open(self, cycle_id, then=None):
        """Read one cycle and draw all of it. ``then`` runs once it is drawn.

        The read is a process started and waited for; with a few hundred runs
        behind a cycle it took long enough to look like a hang. It is asked on
        a thread when the core is a real one (see ``background``), the cycle's
        name goes up at once, and the regions about to be replaced show their
        skeletons until the answer arrives. A click on another cycle meanwhile
        wins: the older answer is dropped when it comes.
        """
        if not cycle_id or self.core is None:
            return
        if (self._draft is not None and self._draft.cycle_id != cycle_id
                and not self._leave_draft()):
            # Stayed: the explorer's selection goes back to what is open.
            self.explorer.select(self.current_id())
            return
        if cycle_id != self.current_id():
            row = next((one for one in self._cycles if one.get("id") == cycle_id), {})
            self.title.setText(row.get("name") or cycle_id)
            self.state.setText("loading...")
            # What the chip says is about the cycle that was open; until the
            # new one is read there is nothing true to say.
            self.validity.setVisible(False)
            self.explorer.select(cycle_id)
            self._loading(("canvas", "output"), True)
        core = self.core
        self._call("open", lambda: core.cycle_show(cycle_id),
                   lambda payload, error: self._opened(cycle_id, payload, error, then))
        if not self._sessions_loaded:
            # Asked beside the cycle rather than after it: the two do not
            # depend on each other, and the list shows it is coming at once.
            self.refresh_sessions(first=cycle_id)

    def _opened(self, cycle_id, payload, error, then=None):
        """The core's answer to :meth:`open`: draw it."""
        self._loading(("canvas", "output"), False)
        if error is not None:
            self._open = None
            self.canvas.set_graph(None)
            self.output.set_problems([str(error)])
            self.state.setText("could not read %s" % cycle_id)
            self._update_buttons()
            return

        # Which cycle is on the canvas, asked before the new payload replaces
        # the answer - and a view still waiting to be written down settled
        # while it is still clear which cycle it belongs to.
        self.cycle_opened.emit(cycle_id)
        drawn = self._drawn_id()
        self._flush_view()
        self._open = payload
        if self._draft is not None and (self._draft.cycle_id != cycle_id
                                        or not self._draft.dirty):
            self._draft = None
        if self._context == RUN and (self._live_cycle() or {}).get("cycle") != cycle_id:
            # A run of another cycle is nothing to show this one by.
            self._set_context(DESIGN)
        self.title.setText(_name_of(payload) or "Cycles")
        self._redraw(drawn)
        # And what it said, not only what it did. Without this, a run restored
        # after a restart came back as a coloured graph beside an empty panel:
        # the model had the stages and the output, and nothing asked for them.
        self._paint_log()
        self._show_text()
        self.explorer.select(cycle_id)
        self.subjects.set_state(cycle=cycle_id, graph=payload.get("cycle"),
                                live=self._live_cycle())
        if self._sessions_loaded and self._sessions_first != cycle_id:
            # The list is paged with the open cycle's sessions first; another
            # cycle open means another first page.
            self._schedule_sessions()
        self._describe_state(payload)
        self._update_buttons()
        if then is not None:
            then()

    # -- asking the core without stopping the window --------------------------
    def _call(self, kind, work, done):
        """Run ``work``; ``done(result, error)`` with its answer - the latest only.

        On a thread when the core's calls block, directly when they do not
        (the fakes the tests use). Asked again before the answer came, the
        earlier answer is dropped: it is about something nobody is looking at
        any more. An error that is not the core's own is raised after
        ``done`` has had its say, so a bug is still a traceback.
        """
        self._asked[kind] = self._asked.get(kind, 0) + 1
        mine = self._asked[kind]

        def answer(result, error):
            if self._asked.get(kind) != mine:
                return
            done(result, error)
            if error is not None and not isinstance(error, core_mod.CoreError):
                raise error

        if background.offloads(self.core):
            background.run(work, answer, owner=self)
        else:
            answer(*background.call(work))

    def _waiting(self, text, work):
        """``work()``'s answer, with the page held and saying ``text`` meanwhile.

        For a call the page cannot go on without - a save, or the check of a
        change - which used to be made on the GUI thread: the window froze for
        as long as the core took, and a frozen window cannot be told from a
        hung one, nor says when it is safe to carry on. The page now takes no
        input until the answer is in, and after a moment the canvas is veiled
        and says what is happening (see widgets.Busy). Raises what ``work``
        raised.
        """
        if not background.offloads(self.core):
            return work()
        self.busy.start(text)
        try:
            result, error = background.wait(work, self.core)
        finally:
            self.busy.stop()
        if error is not None:
            raise error
        return result

    def _loading(self, zones, on):
        """Skeletons on, or off, in these regions. A region folded shut shows
        nothing either way - it has no room to show it in."""
        for zone in zones:
            for name in (zone, zone + "_detail"):
                skeleton = self.skeletons.get(name)
                if skeleton is not None:
                    skeleton.start() if on else skeleton.stop()

    def show_loading(self, on=True):
        """For the window: the canvas and the output are about to be replaced."""
        self._loading(("canvas", "output"), on)

    def inventory_loading(self, on=True):
        """For the window: the list of cycles is being read. Shown only while
        there is nothing in the tree yet - a refresh keeps what is there."""
        if on and not self._cycles:
            self._loading(("explorer",), True)
        elif not on:
            self._loading(("explorer",), False)

    # -- what the canvas shows ----------------------------------------------------
    def _graph_now(self):
        """The graph the canvas should be: the run's, the draft's, or the file's."""
        if self._context == RUN:
            live = self._live_cycle() or {}
            if live.get("graph"):
                return live["graph"]
        if self._draft is not None and self._draft.dirty:
            return ((self._draft.check or {}).get("cycle")
                    or (self._open or {}).get("cycle"))
        return (self._open or {}).get("cycle")

    def _problems_now(self):
        if self._draft is not None and self._draft.dirty:
            return list((self._draft.check or {}).get("problems") or [])
        return list((self._open or {}).get("problems") or [])

    def _redraw(self, drawn=None):
        """Put what the context says on the canvas, keeping the scene if it can.

        The same picture is not a reason to rebuild anything. A click on the
        cycle already open, a Refresh, closing Settings and saving something
        on another page all land here, and rebuilding threw away the statuses
        of a run that was still going, the selection, and wherever the canvas
        had been zoomed and dragged to.
        """
        graph = self._graph_now()
        cycle_id = (graph or {}).get("id") or self.current_id()
        drawn = self._drawn_id() if drawn is None else drawn
        if cycle_id == drawn and _same_shape(graph, self.canvas.graph()):
            self.canvas.update_graph(graph)
        else:
            # Where somebody last put the nodes, and where they were last
            # looking. Anything this does not mention - a step added since, or
            # a cycle never arranged - goes where the layout computes, so a
            # remembered arrangement never has to be thrown away.
            # The same cycle redrawn - a step added, a link drawn - keeps the
            # view on screen. The one in the file is written a second after
            # the view settles, so it can be a zoom or a pan behind, and
            # restoring it moved the picture away from somebody mid-edit.
            same = cycle_id == drawn and self.canvas.graph() is not None
            self.canvas.set_graph(graph, self._remembered(cycle_id),
                                  self.canvas.view() if same
                                  else self._remembered_view(cycle_id),
                                  folded=self._remembered_folded(cycle_id))
            if self._selected and self.canvas.node(self._selected) is None:
                self._selected = ""
        self.canvas.set_problems(self._problems_now())
        # The canvas is drawn from the file, which knows nothing about a run.
        # Whatever the model knows about this cycle is painted back on, so a
        # run still going - or one that finished while somebody was elsewhere -
        # is still there to read.
        self._apply_run_state()

    def _show_text(self):
        """The Problems and YAML tabs: the draft's while there is one."""
        if self._draft is not None and self._draft.dirty:
            check = self._draft.check or {}
            self.output.set_problems(self._problems_now())
            self.output.set_yaml(check.get("text") or "", writable=False)
            self.output.set_yaml_note(
                "This is what Save would write. Save or Revert the changes "
                "made on the canvas to edit the file as text.")
        else:
            self.output.set_problems(self._problems_now())
            self.output.set_yaml((self._open or {}).get("yaml") or "",
                                 bool((self._open or {}).get("writable")))
            self.output.set_yaml_note("")
        self.draft_bar.setVisible(bool(self._draft is not None and self._draft.dirty))
        if self._draft is not None and self._draft.dirty:
            problems = self._problems_now()
            # Short, because it sits on a bar over the canvas; the whole
            # sentence is its tooltip.
            self.draft_note.setText("Unsaved - %d problem%s" % (
                len(problems), "" if len(problems) == 1 else "s")
                if problems else "Unsaved changes")
            self.draft_note.setToolTip(
                "Changes made on the canvas, not in the file yet. %s"
                % ("Fix the %d problem(s) in the Problems tab before saving."
                   % len(problems) if problems else "Save writes them."))
            self.draft_note.setStyleSheet("color: %s;" % (theme.BAD if problems
                                                           else theme.WARN))

    def _statuses_hidden(self):
        """Whether a run's colours would be painted on a graph that did not run.

        In Design the canvas is the file - or changes not saved yet - and a
        run's statuses belong on it only while it is the picture that ran.
        Otherwise they are held back, and Run shows the run's own graph.
        """
        if self._context == RUN:
            return False
        if self._draft is not None and self._draft.dirty:
            return True
        ran = ((self.run_state.cycle or {}) if self.run_state else {}).get("graph")
        return bool(ran) and not _same_shape(ran, self.canvas.graph())

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
        if self._statuses_hidden():
            self.canvas.clear_status()
            return
        for step_id, step in (state.get("steps") or {}).items():
            self.canvas.set_status(step_id, step.get("status"),
                                   step.get("duration_ms"), step.get("attempt"))

    def _context_changed(self, name):
        self._context = name
        self._redraw()
        self._describe_state(self._open or {})
        self._update_buttons()

    def _set_context(self, name):
        """Switch without being asked - a run started, an edit was made."""
        if name != self._context:
            self.context_switch.set_current(name, notify=False)
            self._context = name

    # -- editing: changes collect in a draft until Save ----------------------------
    def _can_edit_graph(self):
        """Whether the open cycle's graph may be changed here at all."""
        return bool(self._open and self._open.get("writable")
                    and self._open.get("document") is not None)

    def _document_now(self):
        """The document edits start from: the draft's, or the file's."""
        if self._draft is not None:
            return self._draft.document
        return (self._open or {}).get("document") or {}

    def _ensure_draft(self):
        if self._draft is None or self._draft.cycle_id != self.current_id():
            self._draft = Draft(self.current_id(), (self._open or {}).get("document"),
                                (self._open or {}).get("digest", ""))
        return self._draft

    def _draft_changed(self):
        """Something about the draft moved: ask the core, and show the answer.

        A dry run of a save, so the problems, the graph and the text are the
        core's own - the same validator, layout and writer a real save uses,
        and no second copy of any of them here.
        """
        draft = self._draft
        if draft is None:
            return
        self._set_context(DESIGN)
        if draft.dirty:
            core, document, base = self.core, draft.document, draft.base
            try:
                draft.check = self._waiting(
                    "Checking...", lambda: core.cycle_save(
                        draft.cycle_id, document, dry_run=True, base=base))
            except core_mod.CoreError as exc:
                draft.check = {"problems": [str(exc)]}
        self._redraw()
        self._show_text()
        self._describe_state(self._open or {})
        self._update_buttons()

    def _undo(self):
        if self._draft is not None and self._draft.undo():
            self._draft_changed()

    def _redo(self):
        if self._draft is not None and self._draft.redo():
            self._draft_changed()

    def _save_shortcut(self):
        if self._draft is not None and self._draft.dirty:
            self._save_draft()
        elif self.output.save_button.isEnabled():
            self._save_yaml(self.output.yaml.toPlainText())

    def _save_draft(self):
        """Write the draft through the core. True when it went."""
        draft = self._draft
        if draft is None or not draft.dirty:
            return True
        if not self._write_cycle(draft.cycle_id, draft.document, base=draft.base):
            return False
        self._draft = None
        self._show_written(draft.cycle_id)
        return True

    def _revert_draft(self):
        if self._draft is None or not self._draft.dirty:
            return
        if not widgets.confirm(self, "Revert", "Throw away the unsaved changes?",
                               "The file is left as it is.", agree="Revert",
                               refuse="Keep them"):
            return
        self._draft = None
        self._redraw()
        self._show_text()
        self._describe_state(self._open or {})
        self._update_buttons()

    def _leave_draft(self):
        """Unsaved changes stand in the way of leaving: ask. True to go on."""
        if self._draft is None or not self._draft.dirty:
            self._draft = None
            return True
        chosen = widgets.choose(
            self, "Unsaved changes",
            "Save the changes to %s?" % self._draft.cycle_id,
            "They were made on the canvas and are not in the file yet.",
            choices=("Discard",), agree="Save", refuse="Cancel")
        if chosen == "Save":
            return self._save_draft()
        if chosen == "Discard":
            self._draft = None
            self._show_text()
            return True
        return False

    def _show_changes(self):
        """What Save would change, as a diff, before it changes it."""
        check = (self._draft.check or {}) if self._draft is not None else {}
        widgets.output(self, "Changes", "What Save would write to %s"
                       % (self._draft.cycle_id if self._draft else ""),
                       check.get("diff") or "(nothing)")

    def _plugins_menu(self, menu, after=""):
        """Every plugin the core offers, by category, each adding a step.

        Saved steps come first: a step somebody configured and kept is the
        one they reach for, and the plugins below are where a new one starts.
        """
        plugins = list(getattr(self.inventory, "cycle_plugins", None) or [])
        self._saved_menu(menu.addMenu("Saved"), after,
                         {one.get("id", ""): one.get("name") or one.get("id", "")
                          for one in plugins})
        menu.addSeparator()
        by_category = {}
        for plugin in plugins:
            by_category.setdefault(plugin.get("category") or "other", []).append(plugin)
        for category in sorted(by_category):
            sub = menu.addMenu(category.title())
            for plugin in sorted(by_category[category], key=lambda one: one.get("id", "")):
                action = sub.addAction(
                    "%s  -  %s" % (plugin.get("id", ""), plugin.get("name", "")),
                    lambda p=plugin.get("id", ""): self._add_step(p, after))
                action.setToolTip(plugin.get("summary") or "")
            sub.setToolTipsVisible(True)
        if not plugins:
            menu.addAction("No plugins are known yet").setEnabled(False)

    def _presets_path(self):
        return presets_file.path_beside(self._projects_path)

    def _saved_menu(self, menu, after, known):
        """The saved steps, each adding a copy of itself, and a way to forget one.

        Read when the menu opens rather than once: the file is small, and a
        list remembered from earlier would miss a step saved from another
        window a minute ago.
        """
        menu.setToolTipsVisible(True)
        try:
            saved = presets_file.load(self._presets_path())
        except presets_file.CyclePresetsFileError as exc:
            menu.addAction("The saved steps could not be read").setEnabled(False)
            menu.setToolTip(str(exc))
            return
        if not saved:
            menu.addAction("Nothing saved yet - right-click a step, "
                           "Save as preset...").setEnabled(False)
            return
        # By the kind of step, each kind under its plugin's name - see
        # cyclepresetsfile.grouped for the order inside one.
        for plugin, presets in presets_file.grouped(saved):
            _menu_heading(menu, known.get(plugin) or plugin)
            for preset in presets:
                action = menu.addAction(
                    preset.name,
                    lambda p=preset: self._add_step(p.plugin, after, p))
                if known and preset.plugin not in known:
                    action.setEnabled(False)
                    action.setToolTip("%s is not a plugin this version knows."
                                      % preset.plugin)
                else:
                    action.setToolTip(preset.plugin)
        menu.addSeparator()
        forget = menu.addMenu("Remove")
        for _plugin, presets in presets_file.grouped(saved):
            for preset in presets:
                forget.addAction(preset.name,
                                 lambda n=preset.name: self._remove_preset(n))

    def _save_preset(self, step_id):
        """Keep this step, as configured, under a name for the Add step menu."""
        step = self._step_of(step_id)
        if step is None:
            return
        name = self._ask("Save as preset", "Name:",
                         step.get("label") or step_id)
        if not name:
            return
        path = self._presets_path()
        try:
            saved = presets_file.load(path)
        except presets_file.CyclePresetsFileError as exc:
            widgets.warn(self, "Save as preset", str(exc))
            return
        same = [one for one in saved if one.name.casefold() == name.casefold()]
        if same and not widgets.confirm(
                self, "Save as preset", "Replace the saved step %s?" % name,
                agree="Replace", refuse="Cancel"):
            return
        made = presets_file.Preset.from_step(name, step, self._document_now())
        # A replaced step keeps its place in the menu.
        if same:
            saved = [made if one is same[0] else one for one in saved
                     if one is same[0] or one not in same]
        else:
            saved.append(made)
        try:
            presets_file.save(path, saved)
        except presets_file.CyclePresetsFileError as exc:
            widgets.warn(self, "Save as preset", str(exc))

    def _remove_preset(self, name):
        if not widgets.confirm(self, "Remove saved step",
                               "Remove the saved step %s?" % name,
                               "Cycles it was added to keep their copy.",
                               agree="Remove", refuse="Keep"):
            return
        path = self._presets_path()
        try:
            saved = presets_file.load(path)
            presets_file.save(path, [one for one in saved if one.name != name])
        except presets_file.CyclePresetsFileError as exc:
            widgets.warn(self, "Remove saved step", str(exc))

    def _add_step_from_bar(self):
        menu = QMenu(self)
        self._plugins_menu(menu, after=self._selected)
        menu.exec(self.add_button.mapToGlobal(self.add_button.rect().bottomLeft()))

    def _add_step(self, plugin_id, after="", preset=None):
        """A new step, filled in straight away in its own window.

        Closing that window without applying takes the step back out - a
        box with nothing in it is not something anybody asked for. A saved
        step arrives already filled in, with the variables it refers to - and
        the values of its secrets, copied from the cycle it was saved from.
        """
        if not self._can_edit_graph():
            return
        draft = self._ensure_draft()
        if preset is not None:
            made = draft.add_step(plugin_id, after=after,
                                  step=preset.step_for(),
                                  variables=preset.variables)
        else:
            made = draft.add_step(plugin_id, after=after)
        if not self._edit_step(made, adding=True):
            draft.undo()
        elif preset is not None:
            self._bring_secrets(preset)
        self._draft_changed()
        if self.canvas.node(made) is not None:
            # Kept where it was put: a step never dragged has no place of its
            # own and was laid out afresh - somewhere else - on every redraw.
            if made in self.canvas.placed_now():
                self._remember_places()
            self.canvas.select(made, center=False)
            self.canvas.reveal(made)

    def _bring_secrets(self, preset):
        """Copy the saved step's secret values from the cycle it came from.

        Inside the store, through the core: the values are never read into
        this window. A secret this cycle already has a value for keeps it - a
        credential somebody set differently on purpose is not overwritten. A
        failure is only said: the step is added either way, and the cycle's
        Properties is where a value can still be typed.
        """
        names = preset.secrets()
        target = self.current_id()
        if (not names or not preset.cycle or preset.cycle == target
                or self.core is None or not target):
            return
        try:
            self.core.cycle_secret_copy(preset.cycle, target, names)
        except core_mod.CoreError as exc:
            widgets.warn(self, "Saved step",
                         "The step was added, but its secrets were not copied "
                         "from %s." % preset.cycle,
                         "Set them in Properties. %s" % exc)

    def _node_menu(self, step_id, where):
        """What can be done to one step, from the step."""
        editable = self._can_edit_graph() and self._context == DESIGN
        document = self._document_now()
        step = next((one for one in document.get("steps") or []
                     if one.get("id") == step_id), None)
        menu = QMenu(self)
        menu.addAction("Open..." if editable else "Show...",
                       lambda: self.open_step(step_id))
        if editable and step is not None:
            menu.addSeparator()
            self._plugins_menu(menu.addMenu("Add a step after it"), after=step_id)
            draft = Draft("", document)
            # Both directions, from the one step: what it waits for, and what
            # waits for it. The second is the same link seen from the other
            # end, and without it making a step come before three others meant
            # opening each of the three.
            needs = menu.addMenu("Waits for")
            before = menu.addMenu("Runs before")
            for submenu in (needs, before):
                submenu.setToolTipsVisible(True)
            needs.setToolTip("The steps this one starts after.")
            before.setToolTip("The steps that start after this one.")
            for other in document.get("steps") or []:
                other_id = other.get("id", "")
                if other_id == step_id:
                    continue
                name = other.get("label") or other_id
                for submenu, waiting, waited_for in ((needs, step_id, other_id),
                                                     (before, other_id, step_id)):
                    action = submenu.addAction(name)
                    action.setCheckable(True)
                    action.setChecked(waited_for in draft.needs_of(waiting))
                    why = draft.why_not_need(waiting, waited_for)
                    allowed = action.isChecked() or not why
                    action.setEnabled(allowed)
                    if not allowed:
                        action.setToolTip(why)
                    action.toggled.connect(
                        lambda on, w=waiting, f=waited_for: self._set_need(w, f, on))
            phases = menu.addMenu("Phase")
            current = (step.get("group") or "").strip()
            for name in [""] + draft.phases():
                action = phases.addAction(name or "(none)",
                                          lambda n=name: self._set_group(step_id, n))
                action.setCheckable(True)
                action.setChecked(name == current)
            phases.addSeparator()
            phases.addAction("New phase...", lambda: self._new_group(step_id))
            menu.addAction("Duplicate", lambda: self._duplicate_step(step_id))
            menu.addAction("Delete...", lambda: self._delete_step(step_id))
        if step is not None:
            # Not only in Design: keeping a step changes nothing in this
            # cycle, and a template that ships with the application is
            # exactly the kind of step worth keeping.
            menu.addAction("Save as preset...",
                           lambda: self._save_preset(step_id))
        menu.addSeparator()
        self._artifacts_menu(menu.addMenu("Artifacts"), step_id)
        running = bool(self.run_state and self.run_state.cycle_running)
        only = menu.addAction("Run this step only", lambda: self._run("only"))
        after = menu.addAction("Run from here", lambda: self._run("from"))
        for action in (only, after):
            action.setEnabled(not running and not (self._draft and self._draft.dirty))
        menu.exec(where)

    def _artifacts_menu(self, menu, step_id):
        """Every file ``step_id`` wrote in the run on screen, and its folder."""
        menu.setToolTipsVisible(True)
        targets, _files = self._artifact_targets(step_id)
        for label, path in targets:
            action = menu.addAction(label, lambda p=path: self._reveal(p))
            action.setToolTip(path)
        if not targets:
            menu.setEnabled(False)
            menu.menuAction().setToolTip(
                "This step wrote nothing in the run on screen." if self._run_dir()
                else "No run of this cycle is on screen to have written any.")

    def _canvas_menu(self, where):
        menu = QMenu(self)
        if self._can_edit_graph() and self._context == DESIGN:
            self._plugins_menu(menu.addMenu("Add a step"))
            menu.addSeparator()
        if self.canvas.phases():
            menu.addAction("Open phases" if self.canvas.folded() else "Fold phases",
                           self._toggle_folding)
        menu.addAction("Fit", self.canvas.fit)
        menu.exec(where)

    def _edge_menu(self, waited_for, waiting, where):
        """What can be done to one link, from the link."""
        menu = QMenu(self)
        said = menu.addAction("%s waits for %s" % (waiting, waited_for))
        said.setEnabled(False)
        menu.addSeparator()
        if self._can_edit_graph() and self._context == DESIGN:
            menu.addAction("Remove this link",
                           lambda: self._remove_link(waited_for, waiting))
            menu.addSeparator()
        for step_id in (waited_for, waiting):
            menu.addAction("Go to %s" % step_id,
                           lambda s=step_id: self.canvas.select(s))
        menu.exec(where)

    def _why_not_link(self, waited_for, waiting, replacing=None):
        """Asked by the canvas of every step a link is dragged over.

        ``replacing`` is the link being re-pointed, if one is - judged as if
        it were already gone, since it will be.
        """
        draft = Draft("", self._document_now())
        if replacing:
            return draft.why_not_move(replacing[1], replacing[0], waiting, waited_for)
        return draft.why_not_need(waiting, waited_for)

    def _link_made(self, waited_for, waiting):
        """A link dragged from one step's dot to another: keep it in the draft."""
        self._set_need(waiting, waited_for, True)
        # Selected, so the new link is picked out - and can be pointed
        # elsewhere or deleted straight away - without the view jumping.
        self.canvas.select_link(waited_for, waiting)

    def _link_moved(self, old_waited_for, old_waiting, waited_for, waiting):
        """One end of a link dragged to another step: one change, one Undo."""
        if self._ensure_draft().move_need(old_waiting, old_waited_for,
                                          waiting, waited_for):
            self._offer_reads(waiting, waited_for)
            self._draft_changed()
            self.canvas.select_link(waited_for, waiting)

    def _remove_link(self, waited_for, waiting):
        self._set_need(waiting, waited_for, False)

    def _set_need(self, step_id, other, wanted):
        if self._ensure_draft().set_need(step_id, other, wanted):
            if wanted:
                self._offer_reads(step_id, other)
            self._draft_changed()

    def _offer_reads(self, waiting, waited_for):
        """``waiting`` now waits for ``waited_for``: should it read from it too?

        Asked only when ``waiting`` reads a step this cycle does not have -
        typically one brought from another cycle, still reading its old
        neighbour by the id it had there. A link is order, a ``${steps...}``
        is data, and drawing one never changed the other, so a run failed on
        a reference the canvas showed as connected. One question per missing
        step; yes rewrites the reference, folded into the link's own change so
        one Undo takes both back.
        """
        draft = self._ensure_draft()
        waiting_name = (draft.step(waiting) or {}).get("label") or waiting
        source_name = (draft.step(waited_for) or {}).get("label") or waited_for
        for missing in draft.missing_reads(waiting):
            if widgets.confirm(
                    self, "Link",
                    "%s reads %s, which is not a step of this cycle. Read it "
                    "from %s instead?" % (waiting_name, missing, source_name),
                    "Its settings will read %s wherever they read %s. Undo "
                    "takes back the link and this together."
                    % (waited_for, missing),
                    agree="Read from %s" % source_name, refuse="Leave it"):
                draft.point_reads(waiting, missing, waited_for, merge=True)

    def _set_group(self, step_id, name):
        if self._ensure_draft().set_group(step_id, name):
            self._draft_changed()

    def _new_group(self, step_id):
        name = self._ask("New phase", "Phase name:")
        if name:
            self._set_group(step_id, name)

    def _duplicate_step(self, step_id):
        made = self._ensure_draft().duplicate_step(step_id)
        if made:
            self._draft_changed()

    def _delete_step(self, step_id):
        draft = self._ensure_draft()
        named = draft.references_to(step_id)
        detail = ("It is taken out of what waits for it. %s still read%s it in "
                  "their settings; the Problems tab will name what to change."
                  % (", ".join(named), "" if len(named) != 1 else "s")
                  if named else "Nothing else names it.")
        if not widgets.confirm(self, "Delete step", "Delete %s?" % step_id,
                               detail + "\n\nIt is not saved until you press Save.",
                               agree="Delete", refuse="Keep"):
            return
        if draft.remove_step(step_id):
            if self._selected == step_id:
                self._selected = ""
            self._draft_changed()

    # -- search (Ctrl+K) -------------------------------------------------------
    def set_listener(self, listener):
        """The task listener, so Search can say which cycles are listening."""
        self._listener = listener

    def search_rows(self):
        """Every cycle with its state; its runs and the plan, as the list has them."""
        from ..cyclesubjects import (plan_id, planned_detail, planned_title,
                                     row_detail, search_state, subject_title)

        found = []
        state = getattr(self, "run_state", None)
        running = ((state.cycle or {}).get("cycle") if state is not None
                   and state.cycle_running else None)
        listener = getattr(self, "_listener", None)
        for row in self._cycles:
            cycle_id = row.get("id", "")
            word, tone, live = "", "neutral", False
            if cycle_id and cycle_id == running:
                word, tone, live = "Running", "accent", True
            elif listener is not None and listener.enabled:
                keys = [key for key in listener.triggers if key[0] == cycle_id]
                if any(listener.problems.get(key) for key in keys):
                    word, tone, live = "Listener problem", "warn", True
                elif keys:
                    word, tone, live = "Listening", "outline", True
            found.append(search.hit(
                row.get("name") or cycle_id,
                " · ".join(part for part in (row.get("project") or "", cycle_id) if part),
                word, tone, keys=row.get("description") or "",
                target=("cycle", cycle_id), live=live))
        for session in self.subjects.sessions():
            word, tone, live = search_state(session)
            subject = session.get("subject") or {}
            found.append(search.hit(
                subject_title(session), " · ".join(part for part in row_detail(session)
                                                   if part),
                word, tone, keys=" ".join(str(part) for part in (
                    session.get("cycle", ""), subject.get("kind", ""))),
                target=("session", session.get("id", "")), live=live))
        for item in self.subjects.planned():
            scheduled = bool(item.get("run_at"))
            found.append(search.hit(
                planned_title(item), planned_detail(item),
                "Scheduled" if scheduled else "Planned",
                "outline" if scheduled else "neutral",
                keys=item.get("cycle", ""),
                target=("planned", plan_id(item), item.get("cycle", "")),
                live=scheduled))
        return found

    def show_found(self, target):
        """Open the cycle; or pick a run or a planned task on the right, as a
        click there would - which opens what that click opens."""
        kind = target[0] if target else ""
        if kind == "cycle":
            if not any(row.get("id") == target[1] for row in self._cycles):
                return False
            self.open(target[1])
            return True
        if kind == "session":
            return self.subjects.reveal(target[1])
        if kind == "planned":
            _kind, key, cycle_id = target
            if cycle_id and cycle_id != self.current_id():
                self.open(cycle_id, then=lambda: self.subjects.reveal(key))
                return True
            return self.subjects.reveal(key)
        return False

    def _find_in(self):
        """What Ctrl+F searches here: a subject's details when the person is in
        them, otherwise the output - while its own tab is the one on show."""
        focus = QApplication.focusWidget()
        details, bar = self.subjects.details, self.subjects.find_bar
        if focus is not None and (focus is details or details.isAncestorOf(focus)
                                  or bar.isAncestorOf(focus)):
            return bar, findbar.target_for(details)
        if self.output.tabs.currentIndex() == TAB_OUTPUT:
            return self.output.find_bar, self.output.find_target()
        return None

    def open_step(self, step_id):
        """Open one step. Double-clicking a node is what calls this."""
        self._edit_step(step_id)

    def _edit_step(self, step_id, adding=False):
        """The step's own window. True when a change was applied.

        What is applied goes into the draft, not the file: Save writes it,
        with every other change made since, after the core has checked the
        lot. In Run, or on a cycle that ships with the application, the
        window only shows the step.
        """
        document = self._document_now()
        if not document or not step_id:
            return False
        step = self._step_of(step_id)
        if step is None:
            return False
        plugin = (self.inventory.cycle_plugin(step.get("plugin", ""))
                  if self.inventory else {})
        state = (self.run_state.cycle or {}) if self.run_state else {}
        editing = self._can_edit_graph() and self._context == DESIGN

        dialog = NodeDialog(
            step, plugin, self, editing,
            live=(state.get("steps") or {}).get(step_id),
            ask_plugin=lambda key, settings: self._ask_plugin(
                step.get("plugin", ""), key, settings),
            trigger=self._trigger_of(step_id),
            groups=Draft("", document).phases(),
            apply_label="Add" if adding else "Apply",
            read_only_note=("This is the step as the run on screen has it. "
                            "Switch to Design to change it."
                            if self._context == RUN else ""))
        if dialog.exec() != QDialog.Accepted or dialog.saved is None:
            return False
        draft = self._ensure_draft()
        probe = Draft("", draft.document)
        probe.replace_step(step_id, dialog.saved)
        new_id = dialog.saved.get("id", step_id)
        # An added step is one change, not an add and then an edit: Undo
        # takes the whole step back out.
        draft.change(_with_listening(probe.document, new_id, new_id,
                                     dialog.saved_trigger), merge=adding)
        if self._selected == step_id and new_id != step_id:
            self._selected = new_id
        if not adding:
            self._draft_changed()
        return True

    def _trigger_of(self, step_id):
        """The trigger that watches this step, as the draft has it, or None."""
        document = self._document_now()
        for one in document.get("triggers") or []:
            if isinstance(one, dict) and one.get("watch") == step_id:
                return one
        return None

    def open_properties(self):
        """The cycle's own name, project and variables."""
        document = (self._open or {}).get("document")
        if not document:
            return
        if self._draft is not None and self._draft.dirty and not self._leave_draft():
            # Properties writes the file at once - secrets follow it - so the
            # canvas's unsaved changes are settled first, one way or the other.
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
        # After the reopen, which sets the cycle's own problems and would
        # otherwise wipe these.
        self.open(cycle_id, then=(lambda: self.output.set_problems(problems))
                  if problems else None)

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
        for step in self._document_now().get("steps") or []:
            if step.get("id") == step_id:
                return step
        return None

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
        phases = self.canvas.current_phases()
        if phases and self.run_state and self.run_state.cycle_running:
            where += " - in %s" % ", ".join(phases)
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

    def refresh_sessions(self, first=None):
        """Ask the core for the sessions and the plan again. Never raises.

        A page of them (``cyclesubjects.PAGE``), the open cycle's first - or
        as many as are on screen already, so a refresh while a run moves does
        not take back the pages somebody scrolled down to load.

        A list that could not be read is left as it was, not a dialog: this
        runs on its own whenever a run moves, and a dialog nobody asked for,
        repeated on every step, would be worse than a list a moment stale.
        Both answers are drawn at once - see ``SubjectsPanel.set_state``.
        """
        if self.core is None:
            return
        if not self._sessions_loaded:
            # Only the first time: a refresh keeps the rows it has on screen
            # until the new ones are ready, rather than flashing grey on every
            # step of a run.
            self._loading(("subjects",), True)
        core = self.core
        first = self.current_id() if first is None else first
        limit = max(SUBJECTS_PAGE, len(self.subjects.sessions()))
        self._sessions_first = first
        # A page still being read is about the list this answer replaces.
        self._asked["sessions_more"] = self._asked.get("sessions_more", 0) + 1

        def work():
            try:
                sessions = core.cycle_sessions(first=first, limit=limit)
            except core_mod.CoreError:
                # A core from before paging refuses the flags: ask it the old
                # way, for everything.
                sessions = core.cycle_sessions()
            try:
                planned = core.cycle_watch_planned()
            except core_mod.CoreError:
                planned = None
            return sessions, planned

        self._call("sessions", work, self._sessions_answered)

    def _sessions_answered(self, answer, error):
        self._loading(("subjects",), False)
        if error is not None:
            return
        sessions_answer, planned_answer = answer
        self._sessions_loaded = True
        sessions = (sessions_answer or {}).get("sessions") or []
        planned = (None if planned_answer is None
                   else (planned_answer or {}).get("planned") or [])
        self.subjects.set_state(
            sessions=sessions,
            planned=KEEP if planned is None else planned,
            live=self._live_cycle(),
            more=bool((sessions_answer or {}).get("more")),
            total=(sessions_answer or {}).get("total") or len(sessions))
        if planned is not None:
            self.planned_loaded.emit(planned)
        # Over every session, not only the page: a cycle whose last run is
        # older than the first fifty sessions still says when it last ran.
        last_runs = (sessions_answer or {}).get("last_runs")
        self.explorer.set_last_runs(_last_runs(sessions) if last_runs is None
                                    else last_runs)
        self._update_buttons()

    def _more_sessions(self):
        """The Subjects list was scrolled to its end: the next page."""
        if self.core is None:
            return
        core, first = self.core, self._sessions_first
        offset = len(self.subjects.sessions())
        self._call("sessions_more",
                   lambda: core.cycle_sessions(first=first, limit=SUBJECTS_PAGE,
                                               offset=offset),
                   self._more_sessions_answered)

    def _more_sessions_answered(self, answer, error):
        if error is not None:
            self.subjects.more_failed()
            return
        answer = answer or {}
        self.subjects.append_sessions(answer.get("sessions") or [],
                                      more=bool(answer.get("more")),
                                      total=answer.get("total") or KEEP)

    def refresh_planned(self):
        """Ask the core what is on the plan. Never raises, for the same reason
        refresh_sessions does not."""
        if self.core is None:
            return
        core = self.core

        def done(answer, error):
            if error is None:
                planned = (answer or {}).get("planned") or []
                self.subjects.set_planned(planned)
                self.planned_loaded.emit(planned)

        self._call("planned", core.cycle_watch_planned, done)

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

        moving = [row for row in self._cycles
                  if (row.get("project") or "").casefold() == old.casefold()]
        if moving and not self._confirm(
                "Rename project",
                "%d cycle(s) name %r. They will be rewritten to name %r."
                % (len(moving), old, name)):
            return

        project.name = name
        # Before the tree is rebuilt, so the renamed group opens or stays shut
        # the way the old one was.
        self.explorer.forget_fold(old, name)
        if not self._write_projects():
            project.name = old
            self.explorer.forget_fold(name, old)
            return
        self.explorer.select_project(name)
        # The cycles are rewritten rather than left pointing at a name that no
        # longer exists - otherwise a rename quietly moves every one of them to
        # Unassigned, which is the Services page's lesson about renaming a stack
        # behind its logs' backs.
        _moved, failed = self._move_cycles([(row["id"], name) for row in moving])
        if failed:
            widgets.warn(self, "Rename project",
                                "Renamed, but these could not be rewritten and "
                                "now show as unassigned:\n\n- %s"
                                % "\n- ".join(failed))

    def _delete_project(self, name):
        project = cpf.find(self._projects, name)
        if project is None:
            return
        orphans = [row for row in self._cycles
                   if (row.get("project") or "").casefold() == name.casefold()]
        extra = ("\n\n%d cycle(s) are in it. The cycles are kept and move to "
                 "%s - their files stop naming the project."
                 % (len(orphans), UNASSIGNED)) if orphans else ""
        if not self._confirm("Delete project",
                             "Delete the project %r?%s" % (name, extra)):
            return
        self._projects = [one for one in self._projects if one is not project]
        if not self._write_projects():
            return
        self.explorer.forget_fold(name)
        # Their files say where they belong, and the tree believes them - so
        # a deleted project whose cycles still named it would come straight
        # back as "not in your projects list". What the question promised is
        # Unassigned, so that is what the files are made to say.
        _moved, failed = self._move_cycles(
            [(row["id"], "") for row in orphans if row.get("writable")])
        kept = [row["id"] for row in orphans if not row.get("writable")] + failed
        if kept:
            widgets.warn(self, "Delete project",
                         "Deleted, but these could not be rewritten and still "
                         "show under %r:\n\n- %s" % (name, "\n- ".join(kept)))

    def _list_project(self, name):
        """Put a project cycles already name into the list - nothing else moves."""
        if not name or cpf.find(self._projects, name) is not None:
            return
        import datetime
        self._projects.append(cpf.ProjectRow(
            name=name, added=datetime.date.today().isoformat()))
        if self._write_projects():
            self.explorer.select_project(name)

    def _settled(self, cycle_id):
        """Whether a file action on ``cycle_id`` may go ahead of its draft."""
        if self._draft is None or self._draft.cycle_id != cycle_id:
            return True
        return self._leave_draft()

    def _move_cycle(self, cycle_id, project):
        """Put a cycle in another project, which rewrites its ``project:``."""
        if not self._settled(cycle_id):
            return
        row = next((one for one in self._cycles if one.get("id") == cycle_id), {})
        if not row.get("writable"):
            widgets.note(self, "Move to project",
                         "%s ships with the application, so its file cannot "
                         "be changed. Duplicate it, and move the copy." % cycle_id)
            return
        _moved, failed = self._move_cycles([(cycle_id, project)])
        if failed:
            widgets.warn(self, "Move to project",
                         "%s could not be rewritten; it stays where it was."
                         % cycle_id)

    def _move_cycles(self, moves):
        """Rewrite ``project:`` in each of ``[(cycle id, project)]``.

        One core call for all of them, behind the page's busy veil, and the
        rows it answers with go straight into the tree - where renaming a
        project of fifty cycles used to be a hundred processes, one after
        another, with the window frozen for all of them. A core from before
        ``--cycle-move`` is asked the old way, one cycle at a time, and the
        inventory read again after. Returns (moved ids, failed ids).
        """
        moves = list(moves)
        if not moves:
            return [], []
        if self.core is None:
            return [], [cycle_id for cycle_id, _project in moves]
        move = getattr(self.core, "cycle_move", None)
        text = "Moving %d cycle%s..." % (len(moves), "" if len(moves) == 1 else "s")
        answer = None
        if move is not None:
            try:
                answer = self._waiting(text, lambda: move(
                    [{"id": cycle_id, "project": project}
                     for cycle_id, project in moves]))
            except core_mod.CoreError:
                answer = None
        if not isinstance(answer, dict) or "moved" not in answer:
            moved = self._waiting(text, lambda: [
                cycle_id for cycle_id, project in moves
                if self._set_cycle_project(cycle_id, project)])
            if moved:
                self.saved.emit()
        else:
            rows = [row for row in answer.get("moved") or [] if row.get("id")]
            moved = [row["id"] for row in rows]
            if rows:
                self._take_rows(add=rows)
        if self.current_id() in moved:
            self.open(self.current_id())
        return moved, [cycle_id for cycle_id, _project in moves
                       if cycle_id not in moved]

    def _take_rows(self, add=(), remove=()):
        """Put rows a write answered with into the list, and drop ``remove``.

        The tree is rebuilt from the list the page already has, and the window
        told through ``cycles_changed`` - what a whole ``--describe`` was read
        for after every write, for the sake of the one row that changed.
        """
        add = [dict(row) for row in add if row.get("id")]
        gone = set(remove) | {row["id"] for row in add}
        self._cycles = sorted([row for row in self._cycles
                               if row.get("id") not in gone] + add,
                              key=lambda row: row.get("id", ""))
        self.explorer.fill(self._cycles, self._projects)
        self._update_buttons()
        self.cycles_changed.emit(list(self._cycles))

    def _set_cycle_project(self, cycle_id, project):
        """Rewrite one cycle's ``project:``, for a core without --cycle-move.
        True when it was written. Called off the GUI thread: no widgets."""
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
            written = self.core.cycle_save(cycle_id, document,
                                           base=payload.get("digest", ""))
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
            payload = self._read_for_edit(cycle_id)
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
        # Made from the original's file, so its comments come along.
        self._write_cycle(new_id, document, then_open=True, source=cycle_id)

    def _rename_cycle(self, cycle_id):
        """Change what a cycle is called - the name on the row, not its id.

        The row shows the name, so renaming from it changes the name. The id is
        the filename and what ``--cycle-run`` is given; changing that is a
        different and rarer thing, and it has its own entry.
        """
        if not self._settled(cycle_id):
            return
        try:
            payload = self._read_for_edit(cycle_id)
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
        self._write_cycle(cycle_id, document, then_open=True,
                          base=payload.get("digest", ""))

    def _change_cycle_id(self, cycle_id):
        """Change the id, which renames the file.

        Written under the new id *before* the old one is removed: if the write
        fails there is still a cycle. Anything that named the old id - a shell
        script, a note, somebody's memory - stops finding it, which is why this
        is not what Rename does.
        """
        if not self._settled(cycle_id):
            return
        try:
            payload = self._read_for_edit(cycle_id)
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
        if not self._write_cycle(new_id, document, then_open=True,
                                 source=cycle_id):
            return
        try:
            removed = self.core.cycle_delete(cycle_id)
        except core_mod.CoreError as exc:
            widgets.warn(self, "Change id",
                                "Written as %s, but %s could not be removed: %s"
                                % (new_id, cycle_id, exc))
            return
        if removed.get("ok", True):
            self._take_rows(remove=[cycle_id])

    def _read_for_edit(self, cycle_id):
        """``cycle_id`` as ``--cycle-show`` answers, for a file action on it.

        The open cycle is not read a second time while its file is the one
        it was opened from; anything else is read behind the busy veil rather
        than on this thread with the window frozen. Raises CoreError.
        """
        if (self._open and self._open.get("id") == cycle_id
                and self._open.get("digest") and not self._changed_since_opened()):
            return dict(self._open,
                        document=dict(self._open.get("document") or {}) or None)
        core = self.core
        return self._waiting("Reading...", lambda: core.cycle_show(cycle_id))

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
        if not self._settled(cycle_id):
            return
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
        # The file is gone: its row goes, and nothing else needs reading.
        self._take_rows(remove=[cycle_id])

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
        if answer.get("row"):
            self._take_rows(add=[answer["row"]])
        else:
            self.saved.emit()

    def _write_cycle(self, cycle_id, document, then_open=False, source="",
                     base=None):
        """Hand a document to the core. Shows what it refused, if it did.

        Nothing is validated here: the core writes nothing that does not hold,
        and a second opinion in the GUI is a second thing to keep in step.

        Two refusals are questions rather than errors. **Stale**: the file
        changed on disk after it was opened here, and writing now would undo
        that change unseen - so nothing is written until somebody chooses.
        **Confirm**: the save would drop comments written in the file (a step
        rewritten whole had one inside it); the diff is shown and the save
        goes ahead only on a yes. ``base`` is the digest the file was read
        with - the open cycle's own unless the caller read it separately.
        """
        if self.core is None:
            return False
        self._written = {}
        options = {}
        digest = base if base is not None else (
            (self._open or {}).get("digest", "") if cycle_id == self.current_id()
            else "")
        if digest:
            options["base"] = digest
        if source:
            options["source"] = source
        core = self.core
        try:
            answer = self._waiting("Saving...", lambda: core.cycle_save(
                cycle_id, document, **options))
            if answer.get("stale"):
                return self._changed_on_disk(cycle_id, answer)
            if answer.get("confirm"):
                if not self._agree_to_lose(answer):
                    return False
                answer = self._waiting("Saving...", lambda: core.cycle_save(
                    cycle_id, document, allow_loss=True, **options))
        except core_mod.CoreError as exc:
            widgets.warn(self, "Save", str(exc))
            return False
        if not answer.get("ok"):
            widgets.note(
                self, "Save", "This was not written:\n\n- %s"
                              % "\n- ".join(str(one) for one in
                                             answer.get("problems") or []))
            return False
        self._written = answer
        if answer.get("row"):
            # The core answered with the cycle's row: put it in the tree now,
            # rather than reading every cycle and scenario on the machine.
            self._take_rows(add=[answer["row"]])
            if then_open:
                self._show_written(cycle_id)
            return True
        if then_open:
            # A saved listener may refresh the inventory immediately.
            self._pending_open = cycle_id
        self.saved.emit()
        return True

    def _show_written(self, cycle_id):
        """Draw ``cycle_id`` as the save just wrote it.

        From the save's own answer when it carries the cycle as an editor
        opens it - a second process for the file written a moment ago is
        what made a save take as long as two. Otherwise, or when leaving a
        draft of another cycle needs asking about first, it is read as usual.
        """
        shown = (self._written or {}).get("shown")
        if (not shown or shown.get("id") != cycle_id
                or (self._draft is not None and self._draft.cycle_id != cycle_id
                    and self._draft.dirty)):
            self.open(cycle_id)
            return
        # Whatever an earlier open is still reading is older than this.
        self._asked["open"] = self._asked.get("open", 0) + 1
        self._opened(cycle_id, shown, None)

    def _changed_on_disk(self, cycle_id, answer):
        """The file moved under us. Reload it, or leave the edit unwritten."""
        if widgets.confirm(
                self, "Changed on disk",
                "%s was changed after it was opened here." % cycle_id,
                "Nothing was written, so the other change is safe. Reload shows "
                "the file as it is now; make your change again on top of it.",
                agree="Reload", refuse="Cancel", kind="warn"):
            self.open(cycle_id)
        return False

    def _agree_to_lose(self, answer):
        """Put a save that would drop comments to the person. True to go ahead."""
        lost = answer.get("lost") or []
        shown = "\n".join(lost[:8]) + ("\n..." if len(lost) > 8 else "")
        return widgets.confirm_output(
            self, "Save", "This save would drop %d comment(s) from the file."
            % len(lost),
            "The change rewrites part of the file that has comments in it, and "
            "they cannot be carried across:\n\n%s\n\nThe whole change is below. "
            "To keep them, cancel and make the change in the YAML tab." % shown,
            text=answer.get("diff") or "", agree="Save without them",
            refuse="Cancel")

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
            self._show_written(cycle_id)

    def _save_yaml(self, text):
        """The YAML tab: the file as somebody typed it."""
        cycle_id = self.current_id()
        if not cycle_id:
            return
        if self._write_cycle(cycle_id, {"yaml": text}):
            self._show_written(cycle_id)

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
        return widgets.ask_text(self, title, label, value) or ""

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
                self, START_OVER, "Start %s over from the beginning?"
                % (_name_of(self._open) or self.current_id()),
                "All steps will be evaluated again in a new run. Paid services "
                "will be called again. Previous runs remain saved.\n\n" + CONSEQUENCE,
                agree=START_OVER)
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
        if self._draft is not None and self._draft.dirty:
            # A run reads the file. Which file that is has to be decided here,
            # by somebody, not found out afterwards from what ran.
            chosen = widgets.choose(
                self, "Unsaved changes",
                "The run would use the file as saved, without your changes.",
                "Save first to run the cycle as it is on screen.",
                choices=("Run the saved version",), agree="Save and run",
                refuse="Cancel")
            if chosen == "Save and run":
                if not self._save_draft():
                    return
            elif chosen != "Run the saved version":
                return
        # Before the question, not after it: asking somebody to agree to
        # twenty-eight steps and then telling them it cannot start anyway
        # wastes the one decision the question exists for.
        problems = (self._open or {}).get("problems") or []
        if problems:
            widgets.note(
                self, "Run", "Fix this first:\n\n- %s"
                             % "\n- ".join(str(one) for one in problems[:6]))
            return
        if not self._agreed(how):
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
        # A run started: the canvas is the run until somebody edits again. A
        # run read back from disk is not starting, and does not take the
        # canvas away from somebody working on the file.
        if self.run_state and self.run_state.cycle_running:
            self._set_context(RUN)
        if _same_shape(graph, self.canvas.graph()):
            self.canvas.clear_status()
        else:
            self._flush_view()
            self.canvas.set_graph(graph, self._remembered(graph.get("id")),
                                  self._remembered_view(graph.get("id")),
                                  folded=self._remembered_folded(graph.get("id")))
            self._selected = ""
        # A run read back from disk arrives whole, after the canvas was drawn:
        # what it knows is painted now, since no step event will follow.
        self._apply_run_state()
        self._paint_log()
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

    def _remembered_folded(self, cycle_id):
        """Which of this cycle's phases were left folded. Never raises."""
        try:
            return clf.folded_for(cycle_id or "")
        except Exception:                 # noqa: BLE001 - cosmetic state only
            return []

    def _folds_changed(self):
        """A phase folded or opened: remember it, and say it on the buttons."""
        try:
            clf.remember_folded(self._drawn_id(), self.canvas.folded())
        except clf.CycleLayoutFileError:
            pass
        if self._selected and self.canvas.node(self._selected) is None:
            # The selected step went into a folded box; it is not a step on
            # the canvas any more, so nothing may act on it as one.
            self._selected = ""
            self._paint_log()
        self._update_buttons()

    def _toggle_folding(self):
        """Fold every phase, or open every one if any is folded."""
        phases = self.canvas.phases()
        self.canvas.set_folded(() if self.canvas.folded() else phases)

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
        if not self._statuses_hidden():
            self.canvas.set_status(step_id, step.get("status"),
                                   step.get("duration_ms"), step.get("attempt"))
        if self._open and self.canvas.phases():
            # Which phase the run is in is part of the line under the title.
            self._describe_state(self._open)
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
        targets, files = self._artifact_targets(self._selected)
        # With nothing picked, or a step that wrote nothing, the button is the
        # way to the run's files by itself - no list holding that one entry.
        self.output.set_artifacts(targets, files, available=bool(self._run_dir()))
        running = bool(self.run_state and self.run_state.cycle_running)
        self.output.set_streaming(running)
        if self._stages_window is not None:
            self._stages_window.set_run_name(state.get("name") or state.get("cycle") or "")
        # The selection only settles who is written to when several steps
        # are working at once; otherwise the composer follows whoever is.
        self.output.composer.set_context(
            state, bool(self.run_state and self.run_state.cycle_running), self._labels(),
            selected=self._selected)

    def step_labels(self):
        """What each step of the drawn cycle is called, by id."""
        return self._labels()

    def selected_step(self):
        """The step selected on the canvas, or "" when none is."""
        return self._selected

    # -- artifacts ------------------------------------------------------------
    def _run_dir(self):
        """Where the run on screen keeps its files, if it is this cycle's run.

        "" otherwise: a step on this canvas has written nothing in a run of
        some other cycle, whatever the step happens to be called there.
        """
        return (self._live_cycle() or {}).get("run_dir") or ""

    def _artifact_targets(self, step_id):
        """``([(label, path)], files)``: what can be opened for ``step_id``.

        The files the step recorded, by name, then the step's own folder.
        Paths are relative to the run's directory, as the core records them.
        Empty with no step, while no run of this cycle is on screen, and for a
        step that wrote nothing.

        Never the run's folder as an entry of its own: the Artifacts page
        always shows the whole run, so the step's folder already lands there,
        with the step picked out - and where there is no step, the Artifacts
        button goes to the run's folder when it is pressed.
        """
        where = self._run_dir()
        if not where or not step_id:
            return [], 0
        step = ((self._live_cycle() or {}).get("steps") or {}).get(step_id) or {}
        targets, seen = [], set()
        for one in step.get("artifacts") or []:
            path = one.get("path") if isinstance(one, dict) else one
            if not path or path in seen:
                continue
            seen.add(path)
            name = (one.get("name") if isinstance(one, dict) else "") or ""
            base = os.path.basename(path)
            targets.append((name if name and name != base else base, path))
        files = len(targets)
        folder = os.path.join("steps", step_id)
        if os.path.isdir(os.path.join(where, folder)):
            targets.append(("Step folder", folder))
        return targets, files

    def _reveal(self, path):
        """Ask for ``path`` of the run on screen on the Artifacts page."""
        where = self._run_dir()
        if where:
            self.artifacts_requested.emit(where, path or "")

    def _open_run_files(self, cycle_id, run_id):
        """A run's folder on the Artifacts page, from its link in Subjects."""
        for session in self.subjects.sessions():
            if session.get("cycle") != cycle_id:
                continue
            for one in session.get("runs") or []:
                if one.get("id") == run_id and one.get("run_dir"):
                    self.artifacts_requested.emit(one["run_dir"], "")
                    return

    # -- Stages in a window of its own ----------------------------------------
    def is_stages_detached(self):
        return self._stages_window is not None

    def detach_stages(self):
        """Put Stages in a window of its own, or bring that window forward."""
        if self._stages_window is not None:
            self._stages_window.bring_forward()
            return self._stages_window
        window = stageswindow.StagesWindow(self.settings)
        window.hold(self.output.take_stage_panel())
        state = (self.run_state.cycle or {}) if self.run_state else {}
        window.set_run_name(state.get("name") or state.get("cycle") or "")
        window.closed_by_person.connect(self.dock_stages)
        # Gone with the page, whatever else happens: the window has no parent
        # to take it down, and a feed nobody can put back is worse than none.
        self.destroyed.connect(window.deleteLater)
        self._stages_window = window
        self.output.set_streaming(bool(self.run_state and self.run_state.cycle_running))
        self._remember_detached(True)
        window.bring_forward()
        self.stages_detached_changed.emit(True)
        return window

    def dock_stages(self):
        """Put Stages back in its tab. A person closing the window does this."""
        window, self._stages_window = self._stages_window, None
        if window is None:
            return
        panel = window.release()
        if panel is not None:
            self.output.put_stage_panel(panel)
        window.closed_by_person.disconnect(self.dock_stages)
        if not window.closed():
            # "Bring back" from the tab: the window goes, and it is the page's
            # doing, so it does not come back round here as a person's close.
            window.shut()
        window.deleteLater()
        self._remember_detached(False)
        self.stages_detached_changed.emit(False)

    def shut_stages_window(self):
        """Close the window because the application is closing.

        Unlike :meth:`dock_stages` this leaves Stages remembered as detached,
        which is what brings the window back on the next start.
        """
        if self._stages_window is not None:
            self._stages_window.shut()

    def show_stages(self):
        """Stages in front of the person, wherever it is. The caller has put
        this page on screen when Stages is a tab."""
        if self._stages_window is not None:
            self._stages_window.bring_forward()
        else:
            self.output.tabs.setCurrentIndex(TAB_STAGES)
            # The output panel dragged shut is a fold, not a choice to never
            # see it: going to Stages opens it.
            if self._down.indexOf(self.output) in self._down.folded():
                self._down.unfold(self._down.indexOf(self.output))
        self.output.stages.setFocus(Qt.ShortcutFocusReason)

    def _remember_folds(self, states):
        try:
            self.settings.save_folds(FOLDS, states)
        except Exception:                # noqa: BLE001 - a view preference
            pass

    def _remember_detached(self, detached):
        if self.settings is not None:
            try:
                self.settings.stages_detached = detached
            except Exception:            # noqa: BLE001 - a view preference
                pass

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
        # Said on the button, not in a line under the header: a line that came
        # and went with Resume moved everything below it, and in a window not
        # tall enough to spare it, resized the window.
        self.run_button.setToolTip(
            "Resume continues this run: what succeeded is kept, failed or\n"
            "unfinished steps are done again - and may call paid services again.\n"
            "%s, in the menu, is a new run from the first step." % START_OVER
            if resume else
            "Start a new run of this cycle.\n"
            "After a failure or a stop this becomes Resume, which continues\n"
            "that run instead.")
        self.intro.setVisible(not has_cycle)
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
        self.arrange_button.setEnabled(drawn and self.canvas.moved()
                                       and not self.canvas.folded())
        dirty = bool(self._draft is not None and self._draft.dirty)
        editing = self._can_edit_graph() and self._context == DESIGN
        self.add_button.setVisible(editing)
        # The dots links are dragged out of: on a cycle open for editing, in
        # Design - never on a run's graph, which is a record of what happened.
        self.canvas.set_linking(self._why_not_link if editing else None)
        live = self._live_cycle() or {}
        self.context_switch.set_enabled(
            RUN, bool(live.get("graph")),
            "No run of this cycle is on screen. Pick one in Subjects, or run it.")
        self.context_switch.setVisible(bool(self._open))
        self.undo_button.setEnabled(bool(self._draft and self._draft.can_undo()))
        self.redo_button.setEnabled(bool(self._draft and self._draft.can_redo()))
        self.save_draft_button.setEnabled(
            dirty and not ((self._draft.check or {}).get("problems") if dirty else ()))
        if dirty:
            # A step run now would run the file, not what is on screen; the
            # run buttons say so by being unavailable until it is saved.
            for button in (self.step_button, self.from_button):
                button.setEnabled(False)
        folded = bool(self.canvas.folded())
        self.fold_button.setVisible(bool(self.canvas.phases()))
        icons.button(self.fold_button, "unfold" if folded else "fold",
                     "Open phases" if folded else "Fold phases")
        self._show_validity()

    def _show_validity(self):
        """The chip over the canvas: whether the file would run, in words."""
        if not self._open:
            self.validity.setVisible(False)
            return
        # What is on the canvas: the draft's verdict while there is one, since
        # that is what Save would write - and "ready to run" only of the file,
        # since the file is what a run reads.
        problems = self._problems_now()
        dirty = bool(self._draft is not None and self._draft.dirty)
        self.validity.setVisible(True)
        if problems:
            icons.button(self.validity, "warn", "%d problem%s" % (
                len(problems), "" if len(problems) == 1 else "s"), theme.BAD)
            self.validity.setStyleSheet("color: %s;" % theme.BAD)
        else:
            icons.button(self.validity, "check",
                         "No problems" if dirty else "Ready to run", theme.OK)
            self.validity.setStyleSheet("color: %s;" % theme.OK)


class _FloatingBar(QFrame):
    """A row of controls laid over the canvas, on a card of its own.

    Its ground is painted here, from ``theme`` at paint time, so it follows
    dark mode like the canvas under it does.
    """

    def __init__(self, *parts):
        QFrame.__init__(self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 8, 6)
        layout.setSpacing(4)
        for part in parts:
            if part is None:
                layout.addStretch(1)
            else:
                layout.addWidget(part)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(QColor(theme.DIVIDER))
        painter.setBrush(QColor(theme.BG))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)
        painter.end()


class _Pinned(QObject):
    """Keeps ``widget`` where ``place(widget, host)`` says, as ``host`` resizes."""

    def __init__(self, widget, host, place):
        QObject.__init__(self, widget)
        self._widget, self._host, self._place = widget, host, place
        host.installEventFilter(self)
        widget.installEventFilter(self)
        self._put()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Resize, QEvent.Show, QEvent.LayoutRequest):
            self._put()
        return False

    def _put(self):
        self._widget.setGeometry(self._place(self._widget, self._host.rect()))
        self._widget.raise_()


def _centred(widget, room):
    """The middle of the canvas, at a width that reads as a paragraph."""
    width = min(room.width() - 48, 520)
    height = widget.heightForWidth(width) if widget.hasHeightForWidth() else 80
    return QRect(room.center().x() - width // 2, room.center().y() - height // 2,
                 max(width, 0), max(height, 0))


def _along_the_foot(widget, room):
    """Across the bottom of the canvas, clear of its edges."""
    width = min(room.width() - 24, max(widget.sizeHint().width(), 420))
    height = widget.sizeHint().height()
    return QRect(room.center().x() - width // 2, room.bottom() - height - 12,
                 max(width, 0), height)


def _menu_heading(menu, text):
    """A group's name in a menu, as the page's own small capitals.

    ``QMenu.addSection`` would be the call, but under the stylesheet it is
    drawn as a bare separator: the name is dropped and only the line is left,
    which says that something changes without saying what. A kicker label is
    the same heading the panels on this page carry, and it is not an item -
    it cannot be hovered, picked or reached with the arrow keys.
    """
    label = widgets.kicker(text)
    label.setContentsMargins(12, 8, 12, 2)
    action = QWidgetAction(menu)
    action.setDefaultWidget(label)
    action.setEnabled(False)
    menu.addAction(action)
    return action


def _last_runs(sessions):
    """``{cycle id: {"when", "status"}}`` - each cycle's most recent run."""
    found = {}
    for session in sessions or ():
        cycle_id = session.get("cycle") or ""
        when = session.get("updated_at") or 0
        if cycle_id and when >= (found.get(cycle_id) or {}).get("when", 0):
            found[cycle_id] = {"when": when, "status": session.get("status") or ""}
    return found


def _framed(canvas, bar=None):
    """The canvas in a panel, so it sits in the page like every other surface,
    with the strip of controls that act on it along its top."""
    holder = QWidget()
    holder.setProperty("role", "panel")
    layout = QVBoxLayout(holder)
    layout.setContentsMargins(1, 1, 1, 1)
    layout.setSpacing(0)
    if bar is not None:
        bar.setContentsMargins(6, 2, 6, 2)
        layout.addWidget(bar)
        layout.addWidget(widgets.hline())
    layout.addWidget(canvas, 1)
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
        # The phase too: it decides which frame a node is drawn in.
        return ([(node.get("id"), node.get("layer"), node.get("row"),
                  node.get("plugin"), node.get("label"), node.get("group") or "")
                 for node in graph.get("nodes") or []],
                [(edge.get("from"), edge.get("to"), edge.get("kind"))
                 for edge in graph.get("edges") or []])

    return shape(one) == shape(other)


class _Output(QWidget):
    """What a step printed, anything wrong with the cycle, and the file itself.

    The YAML tab is editable because the grammar has corners a form should not
    grow a widget for - conditions, ``needs:``, a step's whole ``with:`` - and
    hiding them would make the editor a worse tool than the text editor it
    replaces. That is the same bargain the Scenarios page already strikes.

    The Stages tab can hand its panel to a window of its own
    (``stageswindow``). The tab stays where it is, counting the stages, with a
    line in it saying where they went: the ``TAB_*`` positions are fixed, and
    a tab that vanished would leave nothing to say so.
    """

    save_requested = Signal(str)
    #: The icon on the tab strip, and the one in the placeholder.
    detach_requested = Signal()
    dock_requested = Signal()
    show_window_requested = Signal()
    #: One of the targets set_artifacts was given: a path relative to the run.
    artifact_picked = Signal(str)

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
        # Where the files a step wrote can be looked at: the Artifacts page,
        # on this run, with the file selected. A click goes to the first; the
        # arrow lists them all. With no step picked a click opens the run's
        # folder. Offered only while a run of this cycle is on screen - see
        # set_artifacts.
        self.artifacts_button = icons.button(QToolButton(), "artifacts", "Artifacts")
        self.artifacts_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.artifacts_button.setProperty("hasmenu", "false")
        # Given to the button only while a step has files to list - see
        # set_artifacts.
        self.artifacts_menu = QMenu(self.artifacts_button)
        self.artifacts_button.clicked.connect(self._first_artifact)
        self.artifacts_button.hide()
        self._artifact_targets, self._artifact_files = [], 0
        self._artifacts_available = False
        output_layout.addWidget(widgets.row(None, self.artifacts_button,
                                            self.details_button))
        # Ctrl+F over the summary, or over the log when the details are on.
        self.find_bar = findbar.FindBar()
        output_layout.addWidget(self.find_bar)
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
        self.composer = InteractionComposer()
        # The feed and the box under it, as one thing: this is what moves to
        # a window of its own and back, never a copy of it.
        self.stage_panel = QWidget()
        stage_layout = QVBoxLayout(self.stage_panel)
        stage_layout.setContentsMargins(0, 0, 0, 0)
        stage_layout.addWidget(self.stages, 1)
        stage_layout.addWidget(self.composer)
        # What the tab holds: the panel, or - while a window has it - a line
        # saying so, with the ways to it and back.
        self.stage_host = QWidget()
        self._host_layout = QVBoxLayout(self.stage_host)
        self._host_layout.setContentsMargins(0, 0, 0, 0)
        self._host_layout.setSpacing(0)
        self._host_layout.addWidget(self.stage_panel, 1)
        self.stages_elsewhere = widgets.Elsewhere("Stages are in a separate window")
        self.stages_elsewhere.setContentsMargins(12, 4, 8, 4)
        keys = QLabel(stageswindow.SHORTCUT)
        keys.setProperty("role", "keys")
        keys.setToolTip("Goes to Stages from any window of the application.")
        self.stages_elsewhere.add_widget(keys)
        self.stages_elsewhere.show_requested.connect(self.show_window_requested)
        self.dock_button = _icon_button(
            "dock", "Bring Stages back into this tab.")
        self.dock_button.clicked.connect(self.dock_requested)
        self.stages_elsewhere.add_widget(self.dock_button)
        self.stages_elsewhere.hide()
        self._host_layout.addWidget(self.stages_elsewhere)
        self._host_layout.addStretch(0)
        self.tabs.addTab(output, "Output")
        self.tabs.addTab(self.stage_host, "Stages")
        self.tabs.addTab(self.problems, "Problems")
        # Only its mark, on the tab strip's free end: the words are in the
        # tooltip. Shown while Stages is the tab on show and is here to move.
        self.detach_button = _icon_button(
            "popout", "Detach Stages into a separate window.\n"
            "%s goes to Stages wherever they are." % stageswindow.SHORTCUT)
        self.detach_button.clicked.connect(self.detach_requested)
        self.tabs.setCornerWidget(self.detach_button, Qt.TopRightCorner)
        self.tabs.currentChanged.connect(lambda _index: self._place_detach())
        self._detached = False

        editor = QWidget()
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(6)
        #: Why the text cannot be typed into, when that is not obvious.
        self.yaml_note = widgets.lede("")
        self.yaml_note.setVisible(False)
        editor_layout.addWidget(self.yaml_note)
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
        self._place_detach()

    # -- Stages, here or in a window of its own -------------------------------
    def take_stage_panel(self):
        """Hand the stage panel over to a window; the tab says where it went."""
        self._host_layout.removeWidget(self.stage_panel)
        self._detached = True
        self._host_layout.setStretch(self._host_layout.count() - 1, 1)
        self.stages_elsewhere.show()
        self._place_detach()
        return self.stage_panel

    def put_stage_panel(self, panel):
        """Take the stage panel back from the window it was in."""
        self._host_layout.insertWidget(0, panel, 1)
        panel.show()
        self._detached = False
        self._host_layout.setStretch(self._host_layout.count() - 1, 0)
        self.stages_elsewhere.stop()
        self.stages_elsewhere.hide()
        self._place_detach()

    def is_detached(self):
        return self._detached

    def set_streaming(self, live):
        """Pulse the placeholder while a run is feeding the window, not after."""
        if self._detached and live:
            self.stages_elsewhere.start()
        else:
            self.stages_elsewhere.stop()

    def _place_detach(self):
        self.detach_button.setVisible(
            not self._detached and self.tabs.currentIndex() == TAB_STAGES)

    # -- artifacts ------------------------------------------------------------
    def set_artifacts(self, targets, files=0, available=False):
        """What the Artifacts button opens: ``[(label, path)]`` of one step.

        ``path`` is relative to the run's directory; ``files`` is how many of
        them are files the step recorded, for the count on the button. With
        no targets the button has no list at all and a press opens the run's
        folder - a list whose one entry does what the button does is a second
        way to the same place. ``available`` is whether there is a run to open
        anything of. Rebuilt only when it changes, because this is asked on
        every repaint of the panel and a menu is a set of widgets.
        """
        targets = list(targets or [])
        state = (targets, files, bool(available))
        if state == (self._artifact_targets, self._artifact_files,
                     self._artifacts_available):
            return
        self._artifact_targets, self._artifact_files, self._artifacts_available = state
        self.artifacts_menu.clear()
        for label, path in targets:
            action = self.artifacts_menu.addAction(label)
            action.setToolTip(path)
            action.triggered.connect(
                lambda _checked=False, p=path: self.artifact_picked.emit(p))
        listed = bool(targets)
        self.artifacts_button.setMenu(self.artifacts_menu if listed else None)
        self.artifacts_button.setPopupMode(QToolButton.MenuButtonPopup if listed
                                           else QToolButton.DelayedPopup)
        if self.artifacts_button.property("hasmenu") != ("true" if listed else "false"):
            self.artifacts_button.setProperty("hasmenu", "true" if listed else "false")
            self.artifacts_button.style().unpolish(self.artifacts_button)
            self.artifacts_button.style().polish(self.artifacts_button)
        self.artifacts_button.setText("Artifacts (%d)" % files if files else "Artifacts")
        self.artifacts_button.setToolTip(
            "Open %s on the Artifacts page." % targets[0][0] if listed
            else "Open this run's folder on the Artifacts page.")
        if len(targets) > 1:
            self.artifacts_button.setToolTip(self.artifacts_button.toolTip()
                                             + "\nThe arrow lists the rest.")
        self.artifacts_button.setVisible(bool(available))

    def _first_artifact(self):
        """The first of the step's files, or the run's folder when it has none."""
        self.artifact_picked.emit(self._artifact_targets[0][1]
                                  if self._artifact_targets else "")

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
        self.composer.set_context({}, False)

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
        self.find_bar.retarget(self.find_target())

    def find_target(self):
        """The reader the output shows - the summary or the log - to search."""
        return findbar.target_for(self.output_views.currentWidget())

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

    def set_yaml_note(self, text):
        self.yaml_note.setText(text or "")
        self.yaml_note.setVisible(bool(text))

    def _revert(self):
        self.yaml.setPlainText(self._original)

    def _changed(self):
        """Save and Revert are offered only when there is something to undo."""
        dirty = (self.yaml.toPlainText() != self._original
                 and getattr(self, "_writable", False))
        self.save_button.setEnabled(dirty)
        self.revert_button.setEnabled(dirty)
        self.tabs.setTabText(TAB_YAML, "YAML *" if dirty else "YAML")


def _icon_button(name, tip):
    """A control that is only its mark, with the words in its tooltip."""
    button = icons.button(QToolButton(), name)
    button.setToolButtonStyle(Qt.ToolButtonIconOnly)
    button.setProperty("variant", "icon")
    button.setCursor(Qt.PointingHandCursor)
    button.setToolTip(tip)
    button.setAccessibleName(tip.split("\n")[0])
    return button


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
