"""Launch Sessions: configure a run in the words of the job, not the CLI.

The Command page next door is a control per flag. This page is the same launcher
described the other way round - which environment, whose accounts, what to run,
how much to record - and :mod:`cms_gui.launch` does the translating. Nothing here
builds a command line; it builds a configuration and asks that module for the
argv, which is why a selection made here and the same selection made on the
Command page cannot mean two different things.

The page never shows a path the user has to type, a flag name, or a comma-
separated list to assemble by hand. The one exception is Advanced, folded away,
where a power user who does know what a reports directory is can set one. The
start address is not among them: it is the first thing that differs between two
runs of the same accounts, so it stands under the environment it belongs to.
Where the scenarios live is not among them: that is one answer for the whole
application, so it is Settings -> Scenarios rather than a per-run override.

Which environment, which saved configuration of it, and the working copy of
each are :mod:`cms_gui.launchcontroller`'s to decide; this page shows them and
hands it every change.
"""

import json
import os

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog,
                               QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QLineEdit, QMenu, QPushButton, QRadioButton,
                               QScrollArea, QToolButton, QVBoxLayout, QWidget)

from .. import (icons, launch, launchcontroller as lc, launchprojectsfile as lpf,
               launchpresetsfile as lps, launchvars, load as load_mod, search, theme,
               widgets)
from .launchaccounts import AccountsTable
from .launchsessions import SessionsPanel, label_for as sessions_label_for
from .launchvariables import VariablesPanel

SCENARIO_MODES = [
    (launch.SCENARIOS_NONE, "Open windows",
     "Sign every account in and leave the windows there."),
    (launch.SCENARIOS_ALL, "Run every scenario",
     "Everything the flows folder offers, template and manual ones excluded."),
    (launch.SCENARIOS_PER_USER, "Use each account's own list",
     "Each account runs the scenarios set for it on the Credentials page."),
    (launch.SCENARIOS_PICK, "Choose scenarios",
     "Pick them below. A tag pulls in every scenario carrying it."),
]

#: The scenario radios side by side on one line, so the list under them gets
#: the panel's height; each one's full sentence is its tooltip.
SCENARIO_SHORT = {launch.SCENARIOS_ALL: "Every scenario",
                  launch.SCENARIOS_PER_USER: "Each account's own",
                  launch.SCENARIOS_PICK: "Choose"}

UNSAVED = "(unsaved)"

#: What the windows do once open: the three answers of "What to do".
WHAT_OPEN, WHAT_RUN, WHAT_RECORD = "open", "run", "record"
WHAT_LABELS = {WHAT_OPEN: "Open windows", WHAT_RUN: "Run scenarios",
               WHAT_RECORD: "Record"}
WHAT_HINTS = {
    WHAT_OPEN: "Signs every account in and leaves the windows there.",
    WHAT_RUN: "",
    WHAT_RECORD: "Opens the window with the Scenario Recorder. RUN asks which "
                 "account to record with when several go.",
}
#: Which scenarios "Run scenarios" starts on before one was ever picked: the
#: ones chosen by hand - running every scenario by accident costs the most.
DEFAULT_RUN_MODE = 3
#: The height of Accounts and What to do, both: room for a dozen accounts or
#: scenarios, and still under the address on a 700-pixel screen.
PANEL_HEIGHT = 440
REPORTS_TITLE = "Reports and diagnostics"

class DesktopLinkDialog(QDialog):
    def __init__(self, parent, default_name, default_path, default_icon):
        super().__init__(parent)
        self.setWindowTitle("Configure Desktop Link")
        self.resize(int(parent.width() * 0.5), self.height())
        self.frame = widgets.dress(self)
        layout = QVBoxLayout(self)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)

        grid.addWidget(QLabel("Name:"), 0, 0)
        self.name_edit = QLineEdit(default_name)
        grid.addWidget(self.name_edit, 0, 1)

        grid.addWidget(QLabel("Folder:"), 1, 0)
        path_layout = QHBoxLayout()
        path_layout.setContentsMargins(0, 0, 0, 0)
        self.path_edit = QLineEdit(default_path)
        path_layout.addWidget(self.path_edit)
        browse_btn = icons.button(QPushButton(""), "browse")
        browse_btn.setFixedWidth(38)
        browse_btn.clicked.connect(self._browse)
        path_layout.addWidget(browse_btn)
        grid.addLayout(path_layout, 1, 1)

        grid.addWidget(QLabel("Icon:"), 2, 0)
        icon_layout = QHBoxLayout()
        icon_layout.setContentsMargins(0, 0, 0, 0)
        self.icon_edit = QLineEdit(default_icon)
        icon_layout.addWidget(self.icon_edit)
        browse_icon_btn = icons.button(QPushButton(""), "browse")
        browse_icon_btn.setFixedWidth(38)
        browse_icon_btn.clicked.connect(self._browse_icon)
        icon_layout.addWidget(browse_icon_btn)
        grid.addLayout(icon_layout, 2, 1)

        layout.addLayout(grid)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        save = QPushButton("Save")
        save.setProperty("variant", "primary")
        save.clicked.connect(self.accept)
        btns.addWidget(save)
        layout.addLayout(btns)

    def _browse(self):
        start = self.path_edit.text() or os.path.expanduser("~/Desktop")
        chosen = QFileDialog.getExistingDirectory(self, "Choose folder", start)
        if chosen:
            self.path_edit.setText(chosen)
            
    def _browse_icon(self):
        start = self.icon_edit.text()
        if not start or not os.path.exists(os.path.dirname(start)):
            start = os.path.expanduser("~")
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Choose icon file", start, "Images (*.png *.svg *.ico *.xpm);;All Files (*)"
        )
        if chosen:
            self.icon_edit.setText(chosen)


class LaunchSessionsPage(QWidget):
    """The user-facing launcher: sections, a plain-words summary, and RUN."""

    run_requested = Signal()
    state_changed = Signal()
    #: An environment or a variable was changed from here, in launch-projects.json.
    environments_changed = Signal()
    #: An account was added from here, to users.json: the inventory is stale.
    accounts_changed = Signal()

    def __init__(self, settings, parent=None, registry=None, controller=None):
        super().__init__(parent)
        self.settings = settings
        self.inventory = None
        self.core = None
        if controller is None:
            registry = registry or lpf.Registry(
                lpf.resolve_path(settings.launch_projects_path))
            controller = lc.LaunchController(settings, registry, parent=self)
        self.controller = controller
        self.controller.changed.connect(self._lists_changed)
        self.controller.registry_changed.connect(self.environments_changed.emit)
        # True until _restore() has put the saved state back. Building the
        # controls fires their signals - a checkbox set to its default, a fold
        # restored from settings - and letting those reach _changed() would save
        # the empty default page over the state that is about to be restored.
        self._building = True
        # The saved configuration as it was when it was opened, so an edit made
        # since can be told from the file on disk. None while nothing is opened.
        self._baseline = None
        self._dirty = False
        # This configuration's own start address; "" follows the environment.
        self._own_url = ""
        # Which scenarios "Run scenarios" means (a SCENARIO_MODES index, never
        # 0), kept while Open windows or Record is on so it comes back.
        self._run_sub = DEFAULT_RUN_MODE
        self._recording = False
        # An account + Account just saved, to tick once the inventory has it.
        self._pending_tick = ""
        # The users.json the launcher reads, where + Account writes.
        self.users_path = ""
        # The values this run gives the address's variables (the working
        # copy's: Save keeps them with the configuration).
        self._run_variables = {}
        # What "Choose scenarios" has ticked. The scenario list is shared with
        # "each account's own list", which shows the accounts' scenarios there
        # instead - so the pick is kept here, where showing those cannot touch it.
        self._picked = []
        # The All / Selected view the list had before the accounts' lists took it
        # over; None while they have not.
        self._view_before = None
        self._developer = bool(settings.developer_mode)
        self.variables_panel = VariablesPanel(self)
        self.variables_panel.value_changed.connect(self._variable_changed)
        self.variables_panel.environment_value.connect(self._variable_for_environment)
        self.variables_panel.define_requested.connect(self._variable_defined)

        self._load = load_mod.Sampler()
        self._metric_timer = QTimer(self)
        self._metric_timer.timeout.connect(self._update_metrics)
        self._metric_timer.start(2000)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.scrollArea = QScrollArea()
        self.scrollArea.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.scrollArea.setWidgetResizable(True)
        body = QWidget()
        column = QVBoxLayout(body)
        column.setContentsMargins(24, 20, 24, 20)
        column.setSpacing(6)
        column.addWidget(widgets.heading("Launch Sessions"))
        column.addWidget(widgets.lede(
            "Choose what to open and what to run. Everything technical is worked "
            "out from these answers, so there is no command to write and no file "
            "to edit."))
        column.addSpacing(12)
        # Where: the project, its environment, a configuration of it - the
        # context everything below belongs to, read left to right.
        column.addWidget(self._context_bar())
        column.addSpacing(10)
        column.addWidget(self._address_panel())
        column.addSpacing(14)

        # Who and what: the accounts beside what they do.
        grid = QGridLayout()
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(20)
        # One fixed height for both: what changes inside them - a filter, the
        # answer to What to do - scrolls inside them, and the page under them
        # neither grows nor shrinks, so its scroll position stays put.
        accounts, what = self._accounts_panel(), self._what_panel()
        for panel in (accounts, what):
            panel.setFixedHeight(PANEL_HEIGHT)
        grid.addWidget(accounts, 0, 0)
        grid.addWidget(what, 0, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        column.addLayout(grid)
        column.addSpacing(16)
        # The rest folds away: each only some runs touch.
        column.addWidget(self._logs_fold())
        column.addSpacing(10)
        column.addWidget(self._advanced_panel())
        column.addStretch(1)
        self.scrollArea.setWidget(body)
        outer.addWidget(self.scrollArea, 1)

        outer.addWidget(self._footer())
        self._restore()

    def _on_advanced_toggled(self, expanded):
        if expanded:
            QTimer.singleShot(10, lambda: self.scrollArea.verticalScrollBar().setValue(
                self.scrollArea.verticalScrollBar().maximum()))

    # -- construction ---------------------------------------------------------
    def _context_bar(self):
        """Project, environment, configuration - and the rare things done to it."""
        panel = widgets.BlueprintPanel(padding=(14, 10, 14, 10))
        self.project_combo = widgets.capped(QComboBox())
        self.project_combo.activated.connect(self._project_picked)
        self.env_combo = widgets.capped(QComboBox())
        self.env_combo.activated.connect(self._environment_picked)
        self.config_combo = widgets.capped(QComboBox())
        self.config_combo.activated.connect(self._config_picked)
        for combo in (self.project_combo, self.env_combo, self.config_combo):
            combo.setMinimumWidth(200)
        # Rename, delete, a desktop link, another environment: done to a
        # configuration now and then, never mid-flow - so one unlabelled
        # button, beside the configuration it acts on.
        self.more_button = QToolButton()
        icons.button(self.more_button, "more", "")
        self.more_button.setPopupMode(QToolButton.InstantPopup)
        self.more_button.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.more_button.setProperty("menuglyph", "true")
        self.more_button.setToolTip("More for this configuration")
        self.more_menu = QMenu(self.more_button)
        self.rename_action = QAction("Rename…", self)
        self.rename_action.triggered.connect(self.rename_configuration)
        self.more_menu.addAction(self.rename_action)
        self.use_elsewhere = QAction("Use with another environment…", self)
        self.use_elsewhere.setToolTip(
            "Copy this configuration into another environment - or move it there, "
            "when its own is gone.")
        self.use_elsewhere.triggered.connect(self.use_with_another_environment)
        self.more_menu.addAction(self.use_elsewhere)
        self.desktop_link = QAction("Desktop Link", self)
        self.desktop_link.setCheckable(True)
        self.desktop_link.setToolTip(
            "Put a launcher for this configuration on the desktop.")
        self.desktop_link.triggered.connect(self._toggle_desktop_link)
        self.more_menu.addAction(self.desktop_link)
        self.more_menu.addSeparator()
        self.delete_action = QAction("Delete…", self)
        self.delete_action.triggered.connect(self.delete_configuration)
        self.more_menu.addAction(self.delete_action)
        self.more_button.setMenu(self.more_menu)
        context_row = widgets.row(
            widgets.field("Project", self.project_combo),
            widgets.field("Environment", self.env_combo),
            widgets.field("Configuration", self.config_combo),
            None, self.more_button, spacing=16)
        # Level with the combos, not centred on them and their labels together.
        widgets.level_with(self.config_combo, context_row, self.more_button)
        panel.layout().addWidget(context_row)
        return panel

    def _address_panel(self):
        """The address the windows open, the environment's note under it."""
        panel = widgets.BlueprintPanel(padding=(14, 10, 14, 10))
        # What the page is about: where the windows open. Marked as such.
        panel.setProperty("emphasis", "primary")
        # The address as it will be opened, in one box: what the environment
        # supplies until someone writes their own, and a tag saying which.
        self.url_field = widgets.UrlField()
        self.url_field.edited.connect(self._url_edited)
        self.url_field.value_clicked.connect(self._open_variables)
        self.url_source = widgets.Tag("", "neutral")
        self.url_reset = QPushButton("Reset")
        self.url_reset.setProperty("variant", "ghost")
        self.url_reset.setToolTip("Go back to the environment's address")
        self.url_reset.clicked.connect(lambda: self._url_edited(""))
        self.variables_button = QPushButton("Variables")
        self.variables_button.setProperty("variant", "ghost")
        self.variables_button.setToolTip("The values this address's variables take")
        self.variables_button.clicked.connect(lambda: self._open_variables(""))
        box = widgets.field("Start address", self.url_field)
        # The source and Reset share the label's line, so the address itself
        # keeps the panel's whole width.
        box.layout().removeWidget(box.label)
        box.layout().insertWidget(0, widgets.row(box.label, None, self.url_source,
                                                 self.variables_button, self.url_reset))
        panel.layout().addWidget(box)
        # Under the box it is about, as well as in the footer's list: the
        # footer says RUN refuses, this says what to change and where.
        self.url_problem = QLabel("")
        self.url_problem.setProperty("role", "error")
        self.url_problem.setWordWrap(True)
        self.url_problem.setVisible(False)
        # A name the address uses that nobody defined: defined from here, in
        # the panel, without leaving the page.
        self.url_define = QPushButton("")
        self.url_define.setProperty("variant", "ghost")
        self.url_define.clicked.connect(
            lambda: self._open_variables(self.url_define.property("variable") or ""))
        self.url_define.setVisible(False)
        problem_row = widgets.row(self.url_problem, self.url_define)
        problem_row.layout().setStretch(0, 1)
        panel.layout().addWidget(problem_row)
        self.env_note = widgets.mono("")
        self.env_note.setWordWrap(True)      # an error here must not widen the page
        # A configuration whose environment is gone can only be put right by
        # moving it - choosing another environment above goes there instead.
        self.env_move = QPushButton("Move to…")
        self.env_move.setProperty("variant", "ghost")
        self.env_move.setToolTip("This configuration's environment is gone: move it "
                                 "to one that exists.")
        self.env_move.clicked.connect(self.use_with_another_environment)
        self.env_move.setVisible(False)
        note_row = widgets.row(self.env_note, self.env_move)
        note_row.layout().setStretch(0, 1)       # the note takes the line it wraps in
        panel.layout().addWidget(note_row)
        return panel

    def _accounts_panel(self):
        panel = widgets.BlueprintPanel()
        self.users_mode = widgets.Segmented(["All accounts", "Choose accounts"])
        self.users_mode.changed.connect(lambda _v: self._users_mode_changed())
        self.add_account = QPushButton("+ Account")
        self.add_account.setToolTip("Add an account to this environment and choose it")
        self.add_account.clicked.connect(self._add_account)
        panel.layout().addWidget(widgets.row(widgets.kicker("Accounts"), None,
                                             self.users_mode, self.add_account))
        self.users_list = AccountsTable()
        self.users_list.changed.connect(self._users_changed)
        panel.layout().addWidget(self.users_list, 1)
        return panel

    def _what_panel(self):
        """What the windows do once they are open - and only what applies to it."""
        panel = widgets.BlueprintPanel()
        self.what = widgets.Segmented(list(WHAT_LABELS.values()), WHAT_LABELS[WHAT_OPEN])
        self.what.changed.connect(lambda _v: self._what_changed())
        panel.layout().addWidget(widgets.row(widgets.kicker("What to do"), None,
                                             self.what))
        # Everything under the switch scrolls inside the panel, whose height
        # is fixed: changing the answer changes what is in it, never the page.
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 4, 0)
        body.setSpacing(6)
        self.what_scroll = QScrollArea()
        self.what_scroll.setFrameShape(QFrame.NoFrame)
        self.what_scroll.setWidgetResizable(True)
        self.what_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.what_scroll.setWidget(content)
        self.what_hint = QLabel("")
        self.what_hint.setProperty("role", "hint")
        self.what_hint.setWordWrap(True)
        body.addWidget(self.what_hint)
        # Which scenarios: the radio group is the one truth for the rules. Its
        # first button is Open windows, never shown -
        # the segmented control above says it.
        self.scenario_mode = QButtonGroup(self)
        self.scenario_mode.setExclusive(True)
        self._scenario_buttons = []
        radios = []
        for index, (mode, label, hint) in enumerate(SCENARIO_MODES):
            button = QRadioButton(SCENARIO_SHORT.get(mode, label))
            button.setToolTip("%s: %s" % (label, hint))
            self.scenario_mode.addButton(button, index)
            self._scenario_buttons.append(button)
            if index:
                radios.append(button)
        self.scenario_row = widgets.row(*radios, None, spacing=18)
        body.addWidget(self.scenario_row)
        self.scenario_mode.button(0).setChecked(True)
        self.scenario_mode.idToggled.connect(self._scenario_mode_changed)
        # What a recording writes to: a scenario of one's own to continue - its
        # steps are loaded and what is captured is added - or a new one.
        self.record_into = widgets.capped(QComboBox())
        self.record_into.currentIndexChanged.connect(self._changed)
        self.record_box = widgets.field(
            "Record into", self.record_into,
            "Continuing loads the scenario's steps and adds what you capture.")
        body.addWidget(self.record_box)
        self.scenario_list = widgets.CheckList(placeholder="Search scenarios and tags…",
                                               selected_view=True)
        self.scenario_list.set_noun("scenarios")
        # Run scenarios has to fit the panel's fixed height with no scroll of
        # its own; the list takes whatever room the one-line rows leave.
        self.scenario_list.list.setMinimumHeight(140)
        self.scenario_list.changed.connect(self._scenario_list_changed)
        body.addWidget(self.scenario_list, 1)

        # How many at once: set once, rarely touched - so one button saying
        # what is in force, and the number in a panel under it.
        self.sessions_panel = SessionsPanel(self)
        self.jobs = self.sessions_panel.jobs
        self.jobs.valueChanged.connect(self._changed)
        self.jobs_all = self.sessions_panel.jobs_all
        self.jobs_all.toggled.connect(self._jobs_all_toggled)
        self.jobs_auto = self.sessions_panel.jobs_auto
        self.jobs_auto.toggled.connect(self._jobs_auto_toggled)
        self.metrics_warning = self.sessions_panel.metrics
        self.sessions_button = QPushButton("")
        self.sessions_button.setProperty("variant", "ghost")
        icons.button(self.sessions_button, "unfold")
        self.sessions_button.setLayoutDirection(Qt.RightToLeft)    # chevron after
        self.sessions_button.setToolTip("How many windows run at once")
        self.sessions_button.clicked.connect(
            lambda: self.sessions_panel.open_below(self.sessions_button))
        sessions_label = QLabel("Sessions at once")
        # And on the same line, what becomes of the windows: two settings of
        # one line each, not two lines taken from the list above.
        self.keep_open = QCheckBox("Keep windows open")
        self.keep_open.setToolTip("Leave the windows open when the run finishes")
        self.keep_open.setChecked(True)
        self.keep_open.toggled.connect(self._changed)
        self.sessions_box = widgets.row(sessions_label, self.sessions_button, None,
                                        self.keep_open)
        body.addWidget(self.sessions_box)
        # Memory pressure is worth seeing without opening the panel.
        self.load_warning = QLabel("")
        self.load_warning.setProperty("role", "error-bold")
        self.load_warning.setWordWrap(True)
        self.load_warning.setVisible(False)
        body.addWidget(self.load_warning)
        # Takes the room only when the scenario list is hidden (it stretches
        # otherwise), so the answers stay at the top beside the accounts.
        body.addStretch(0)
        panel.layout().addWidget(self.what_scroll, 1)
        return panel

    def _reports_section(self, section):
        """Reports, screenshots, the guide, the overlay, the lock: a scripted run's.

        Greyed rather than hidden when nothing is run, so Advanced keeps its
        height whatever What to do says.
        """
        self.reports_note = QLabel("only when running scenarios")
        self.reports_note.setProperty("role", "hint")
        section.addWidget(widgets.row(widgets.kicker(REPORTS_TITLE), None,
                                      self.reports_note))
        self.reports_box = QWidget()
        inner = QVBoxLayout(self.reports_box)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(10)
        section.addWidget(self.reports_box)
        self.report_mode = widgets.Segmented(["Results only", "Full diagnostics",
                                             "Choose"])
        self.report_mode.changed.connect(lambda _v: self._report_mode_changed())
        inner.addWidget(self.report_mode)
        self.report_list = widgets.CheckList(searchable=False)
        self.report_list.set_noun("artifacts")
        self.report_list.changed.connect(self._changed)
        inner.addWidget(self.report_list)
        self.shots = widgets.capped(QComboBox())
        for mode in (launch.SHOTS_OFF, launch.SHOTS_FINISH,
                     launch.SHOTS_START_FINISH, launch.SHOTS_EACH):
            self.shots.addItem(launch.SHOT_LABELS[mode], mode)
        self.shots.currentIndexChanged.connect(self._changed)
        inner.addWidget(widgets.field("Screenshots", self.shots))
        self.report_always = QCheckBox("Keep the full report even when everything "
                                       "passes")
        self.report_always.toggled.connect(self._changed)
        inner.addWidget(self.report_always)
        # The guide is a document for people, not a diagnostic: its own switch,
        # read in Artifacts and exported to PDF from there.
        self.report_doc = QCheckBox("Write a step-by-step guide while it runs")
        self.report_doc.setToolTip(
            "Each scenario leaves documentation.html in its report folder: the "
            "steps that have a doc:, grouped by page, with pictures. Artifacts "
            "shows it and exports it to PDF.")
        self.report_doc.toggled.connect(self._changed)
        inner.addWidget(self.report_doc)
        self.overlay = QCheckBox("Show an execution overlay inside each window")
        self.overlay.toggled.connect(self._overlay_toggled)
        inner.addWidget(self.overlay)
        self.overlay_list = widgets.CheckList(searchable=False)
        self.overlay_list.set_noun("overlay parts")
        self.overlay_list.changed.connect(self._changed)
        inner.addWidget(self.overlay_list)
        self.lock_windows = QCheckBox("Lock the windows while scenarios run")
        self.lock_windows.setToolTip(
            "While a window's scenarios run, its page ignores your mouse and "
            "keyboard, so a stray click cannot steer the run. It unlocks when they "
            "finish. The browser's own tabs, address bar and close button still "
            "work; a tab the scenario opens is not locked, and the execution "
            "overlay cannot be scrolled until then.")
        self.lock_windows.toggled.connect(self._changed)
        inner.addWidget(self.lock_windows)
        inner.addWidget(self._folder_field("reports_dir", "Reports folder"))

    def _extensions_section(self, inner):
        """The browser add-ons, at the end of Advanced: set once, then left."""
        self.ext_mode = widgets.Segmented(["All", "None", "Choose"])
        self.ext_mode.changed.connect(lambda _v: self._ext_mode_changed())
        inner.addWidget(widgets.row(widgets.kicker("Extensions"), None, self.ext_mode))
        inner.addWidget(widgets.lede(
            "Browser add-ons installed into every profile before it opens."))
        # Not one of the list's: the launcher's own, in every profile whatever
        # the list says - it is what signs each account in. Off, the windows
        # open on the sign-in form, for a person or a scenario to fill.
        self.autologin = QCheckBox("Auto-login  -  signs each window in with its "
                                   "account")
        self.autologin.setChecked(True)
        self.autologin.setToolTip(
            "Fills and submits the sign-in form of the environment's address once "
            "per tab. Turn it off for a scenario that signs in by itself; a "
            "profile still signed in from before stays signed in (Empty sessions "
            "starts it signed out).")
        self.autologin.toggled.connect(self._changed)
        # Inset to the list's own tick boxes, so it reads as the row above them.
        inset = QWidget()
        inset_row = QHBoxLayout(inset)
        inset_row.setContentsMargins(5, 0, 0, 0)
        inset_row.addWidget(self.autologin)
        inner.addWidget(inset)
        self.ext_list = widgets.CheckList(searchable=False)
        self.ext_list.set_noun("extensions")
        self.ext_list.changed.connect(self._changed)
        inner.addWidget(self.ext_list)

    def _logs_fold(self):
        panel = widgets.BlueprintPanel()
        self.logs_fold = widgets.Disclosure("Server logs")
        panel.layout().addWidget(self.logs_fold)
        inner = self.logs_fold.body()
        self.logs_mode = widgets.Segmented(["Off", "Default", "All", "Choose"])
        self.logs_mode.changed.connect(lambda _v: self._logs_mode_changed())
        inner.addWidget(widgets.row(self.logs_mode, None))
        inner.addWidget(widgets.lede(
            "The backend's own log, streamed into each session's panel and shown "
            "only the lines written after that window opened."))
        self.logs_list = widgets.CheckList(searchable=False)
        self.logs_list.set_noun("logs")
        self.logs_list.list.setMinimumHeight(96)
        self.logs_list.changed.connect(self._changed)
        inner.addWidget(self.logs_list)
        self.logs_note = QLabel("")
        self.logs_note.setProperty("role", "hint")
        self.logs_note.setWordWrap(True)
        inner.addWidget(self.logs_note)
        return panel

    def _logs_mode_changed(self):
        if not self._building and self.logs_mode.current() == "Choose" \
                and not self.logs_list.checked():
            self.logs_list.set_all(True)      # "choose" starts from everything
        self._changed()

    def _logs_mode_value(self):
        return {"Default": launch.LOGS_DEFAULT, "All": launch.LOGS_ALL,
                "Choose": launch.LOGS_PICK}.get(self.logs_mode.current(),
                                                launch.LOGS_OFF)

    def _populate_server_logs(self, keep):
        """The logs configured for the chosen environment, with ``keep`` ticked.

        Filtered by environment on purpose: offering the staging stand's nginx
        while launching localhost is offering something the core will reject.
        """
        env_value = self._env_filter()
        rows = []
        if self.inventory:
            for row in self.inventory.logs_for(env_value or None):
                note = row.get("target", "") or row.get("type", "")
                if row.get("default"):
                    note = "default   %s" % note
                rows.append((row.get("name", ""), note))
        known = {name for name, _note in rows}
        # Kept rather than dropped: silently editing a saved configuration the
        # moment it is opened is worse than showing a row that will be skipped.
        self._logs_here = len(rows)
        for name in keep or ():
            if name not in known:
                rows.append((name, "(not configured for this environment)"))
        self.logs_list.clear()
        for name, note in rows:
            self.logs_list.add(name, name, note)
        self.logs_list.set_checked(keep)
        self._logs_carried = len(rows) - self._logs_here
        self._update_logs_note(self.detach.isChecked())

    def _update_logs_note(self, detached):
        """One place for every reason this block might have nothing to offer.

        Each has to be able to clear the others: unticking fire-and-forget must not
        leave its explanation behind, and neither must a refresh that finds logs.
        """
        here = getattr(self, "_logs_here", 0)
        carried = getattr(self, "_logs_carried", 0)
        path = self.inventory.log_sources_path if self.inventory else ""
        if detached:
            text = ("Not available with \"leave the windows running after the "
                    "launcher exits\": the log readers belong to this launcher, and "
                    "they stop when it does.")
        elif not here and not carried:
            text = ("No server logs configured%s. Describe your connections and log "
                    "files on the Log sources page." % (" in %s" % path if path else ""))
        elif not here:
            # The state behind an empty report: the saved configuration names logs
            # that belong to another environment, so the run streams nothing. Said
            # here rather than left to a warning after the windows are already open.
            text = ("None of these logs is configured for this environment, so "
                    "nothing will be streamed. Add the environment to them on the "
                    "Log sources page, or pick different ones.")
        else:
            text = ""
        self.logs_note.setText(text)
        self.logs_note.setVisible(bool(text))

    def _update_metrics(self):
        """Say what the machine is doing. Never touch the user's settings for them.

        This deliberately does not adjust `jobs`: a number the user typed and
        saved is theirs, and silently winding it down every two seconds the CPU
        is busy edits a saved configuration behind their back - while the run is
        under way, which is exactly when the CPU is busy. Capping windows to what
        the machine can carry is the load governor's job, in the core, where it
        can act on the real thing instead of on a spin box.
        """
        load = self._load.read()
        if not load.readable:
            self.metrics_warning.setText("System load is not readable on this platform.")
            return
        used = load.used_percent
        strained = (load.mem_stall is not None and load.mem_stall > 10) or \
                   (used is not None and used > 90)
        if strained:
            note = ("Memory is under pressure. More sessions than the machine can hold "
                    "will be opened a few at a time.")
        else:
            note = "System load: CPU %s | RAM %s" % (
                "--" if load.cpu_percent is None else "%.0f%%" % load.cpu_percent,
                "--" if used is None else "%.0f%%" % used)
        self.metrics_warning.setProperty("role", "error-bold" if strained else "hint")
        self.metrics_warning.style().unpolish(self.metrics_warning)
        self.metrics_warning.style().polish(self.metrics_warning)
        self.metrics_warning.setText(note)
        self.load_warning.setText(note if strained else "")
        self.load_warning.setVisible(strained and self.sessions_box.isVisibleTo(self))

    def _advanced_panel(self):
        panel = widgets.BlueprintPanel()
        self.advanced = widgets.Disclosure("Advanced")
        self.advanced.toggled.connect(self._on_advanced_toggled)
        panel.layout().addWidget(self.advanced)
        inner = self.advanced.body()
        inner.addWidget(widgets.lede(
            "Overrides most runs never need. Left empty, each of these follows "
            "the launcher's own default."))

        # Groups on a faint ground each, so they read apart: what a run
        # leaves behind, where the profiles live, how the windows and the
        # launcher behave, what is installed into the windows.
        self.dir_edits = {}
        self._reports_section(self._advanced_section(inner, None))
        profiles = self._advanced_section(inner, "Profiles")
        self.profile_prefix = QLineEdit()
        self.profile_prefix.setPlaceholderText("named after the environment")
        self.profile_prefix.textChanged.connect(self._changed)
        line = widgets.row(widgets.field("Profile folder prefix", self.profile_prefix),
                           self._folder_field("sessions_dir", "Profiles folder"),
                           spacing=16)
        line.layout().setStretch(0, 1)
        line.layout().setStretch(1, 1)
        profiles.addWidget(line)

        windows = self._advanced_section(inner, "Windows and launcher")
        self.maximized = QCheckBox("Open the windows full screen (maximized)")
        self.maximized.toggled.connect(self._changed)
        windows.addWidget(self.maximized)
        self.no_password_prompts = QCheckBox("Don't offer to save passwords")
        self.no_password_prompts.setToolTip(
            "Chrome neither offers to save the password an account signs in with "
            "nor warns that it was found in a data breach. Sign-in is unaffected: "
            "the launcher fills the form itself.")
        self.no_password_prompts.toggled.connect(self._changed)
        windows.addWidget(self.no_password_prompts)
        self.detach = QCheckBox("Leave the windows running after the launcher exits")
        self.detach.toggled.connect(self._changed)
        windows.addWidget(self.detach)
        self.log_level = widgets.capped(QComboBox(), 200)
        for level in launch.LOG_LEVELS:
            self.log_level.addItem(level)
        self.log_level.setCurrentText("INFO")
        self.log_level.currentTextChanged.connect(self._changed)
        windows.addWidget(widgets.row(widgets.field("Log detail", self.log_level), None))

        self._extensions_section(self._advanced_section(inner, None))
        for section in self._advanced_sections:
            widgets.see_through(section)
        return panel

    def _advanced_section(self, inner, title):
        """A group of Advanced on its own faint ground; its layout to fill."""
        frame = QFrame()
        frame.setProperty("role", "section")
        self._advanced_sections = getattr(self, "_advanced_sections", []) + [frame]
        column = QVBoxLayout(frame)
        column.setContentsMargins(12, 10, 12, 12)
        column.setSpacing(8)
        if title:
            column.addWidget(widgets.kicker(title))
        inner.addWidget(frame)
        return column

    def _folder_field(self, key, label):
        """A folder box with its Browse button, kept in ``dir_edits``."""
        edit = QLineEdit()
        edit.setProperty("mono", True)
        edit.setPlaceholderText("(default)")
        edit.textChanged.connect(self._changed)
        browse = icons.button(QPushButton(""), "browse")
        browse.setFixedWidth(38)
        browse.clicked.connect(lambda _c=False, e=edit: self._browse(e))
        self.dir_edits[key] = edit
        return widgets.field(label, widgets.row(edit, browse))

    def _footer(self):
        footer = QFrame()
        footer.setProperty("role", "footer")
        column = QVBoxLayout(footer)
        column.setContentsMargins(24, 10, 24, 10)
        column.setSpacing(6)

        # Where this run is, in one line: always shown, unlike the summary.
        self.context_line = widgets.ElidedLabel(mode=Qt.ElideMiddle)
        self.context_line.setProperty("role", "summary")
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.summary.setProperty("role", "summary")
        self.reset_button = reset = QPushButton("Reset")
        reset.clicked.connect(self.reset)
        # Said in words as well as by the red Save: a colour alone is easy to
        # read past, and an unsaved edit is the thing that must not be missed.
        self.unsaved_note = QLabel("Changes not saved")
        self.unsaved_note.setProperty("role", "hint")
        self.unsaved_note.setVisible(False)
        self.footer_save = save = QPushButton("Save")
        save.clicked.connect(self.save_configuration)
        self.save_as_button = save_as = QPushButton("Save as…")
        save_as.setToolTip("Save these settings as a new configuration of this "
                           "environment; the open one keeps what it has saved.")
        save_as.clicked.connect(self.duplicate_configuration)
        # theme.BG, not the default ink: a primary button paints its label in
        # the background colour against the accent fill, and the mark beside it
        # has to be the same colour or it reads as a different control.
        self.run_button = icons.button(QPushButton("RUN"), "run", color=theme.BG)
        self.run_button.setProperty("variant", "primary")
        self.run_button.clicked.connect(self.run_requested.emit)
        # The footer grows with what it has to say - summary, notes, the command
        # preview - and on a small screen it can take a third of the page. The
        # buttons never fold away, so RUN is always one click from anywhere.
        self.summary_toggle = QPushButton("")
        self.summary_toggle.setProperty("variant", "ghost")
        self.summary_toggle.setCheckable(True)
        self.summary_toggle.setCursor(Qt.PointingHandCursor)
        self.summary_toggle.toggled.connect(self._summary_toggled)
        strip = QHBoxLayout()
        strip.setContentsMargins(0, 0, 0, 0)
        strip.setSpacing(14)
        strip.addWidget(self.summary_toggle)
        strip.addWidget(self.context_line, 1)
        strip.addWidget(self.unsaved_note)
        for button in (reset, save, save_as, self.run_button):
            strip.addWidget(button)
        column.addLayout(strip)
        column.addWidget(self.summary)

        # Problems are never folded away: they are the reason RUN would refuse,
        # and a refusal with its explanation hidden is the worst of both.
        self.problems = QLabel("")
        self.problems.setWordWrap(True)
        self.problems.setProperty("role", "error")
        column.addWidget(self.problems)
        self.notes = QLabel("")
        self.notes.setWordWrap(True)
        self.notes.setProperty("role", "hint")
        column.addWidget(self.notes)
        self.preview = QLabel("")
        self.preview.setWordWrap(True)
        self.preview.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.preview.setProperty("role", "preview")
        column.addWidget(self.preview)
        self.summary_toggle.setChecked(self.settings.launch_summary_expanded)
        self._summary_toggled(self.summary_toggle.isChecked())
        return footer

    # -- state ----------------------------------------------------------------
    def state(self):
        """The configuration as the user has it, in Launch Sessions' own words."""
        environment, value = self.controller.environment_fields()
        return {
            "environment": environment,
            "environment_value": value,
            "variables": dict(self._run_variables),
            "users": {"mode": (launch.USERS_PICK
                               if self.users_mode.current() == "Choose accounts"
                               else launch.USERS_ALL),
                      "logins": self.users_list.checked()},
            "sessions": {"jobs": self.jobs.value(),
                         "all_at_once": self.jobs_all.isChecked(),
                         "auto_jobs": self.jobs_auto.isChecked(),
                         "keep_open": self.keep_open.isChecked(),
                         "detach": self.detach.isChecked(),
                         "maximized": self.maximized.isChecked(),
                         "no_password_prompts": self.no_password_prompts.isChecked(),
                         "lock_windows": self.lock_windows.isChecked()},
            "extensions": {"mode": self._ext_mode_value(),
                           "names": self.ext_list.checked(),
                           "autologin": self.autologin.isChecked()},
            "server_logs": {"mode": self._logs_mode_value(),
                            "names": self.logs_list.checked()},
            "scenarios": {"mode": self._scenario_mode_value(),
                          "run_mode": SCENARIO_MODES[self._run_sub][0],
                          "selected": (self.scenario_list.checked()
                                       if self._scenario_mode_value()
                                       == launch.SCENARIOS_PICK
                                       else list(self._picked))},
            "record": self._what_value() == WHAT_RECORD,
            "record_into": self.record_into.currentData() or "",
            "reports": {"level": self._report_mode_value(),
                        "artifacts": self.report_list.checked(),
                        "always": self.report_always.isChecked(),
                        "doc": self.report_doc.isChecked()},
            "screenshots": {"mode": self.shots.currentData() or launch.SHOTS_OFF},
            "overlay": {"enabled": self.overlay.isChecked(),
                        "components": self.overlay_list.checked()},
            "advanced": {"url": self._own_url,
                         "profile_prefix": self.profile_prefix.text().strip(),
                         "log_level": self.log_level.currentText(),
                         "reports_dir": self.dir_edits["reports_dir"].text().strip(),
                         "sessions_dir": self.dir_edits["sessions_dir"].text().strip()},
        }

    def set_state(self, config):
        """Load a configuration into the controls (from a save or from history)."""
        config = launch.merged(config)
        self._building = True
        try:
            # The environment is not a control's value any more but the place
            # the page is at - the controller's - so only the rest is loaded.
            self._run_variables = dict(config.get("variables") or {})

            users = config["users"]
            self.users_mode.set_current(
                "Choose accounts" if users["mode"] == launch.USERS_PICK
                else "All accounts", notify=False)
            self._populate_users(users["logins"])

            sessions = config["sessions"]
            self.jobs.setValue(max(1, int(sessions["jobs"] or 1)))
            self.jobs_all.setChecked(bool(sessions["all_at_once"]))
            self.jobs_auto.setChecked(bool(sessions["auto_jobs"]))
            self.keep_open.setChecked(bool(sessions["keep_open"]))
            self.detach.setChecked(bool(sessions["detach"]))
            self.maximized.setChecked(bool(sessions["maximized"]))
            self.no_password_prompts.setChecked(bool(sessions["no_password_prompts"]))
            self.lock_windows.setChecked(bool(sessions["lock_windows"]))

            extensions = config["extensions"]
            self.ext_mode.set_current({launch.EXT_NONE: "None",
                                       launch.EXT_PICK: "Choose"}.get(
                                          extensions["mode"], "All"), notify=False)
            self._populate_extensions(extensions["names"])
            self.autologin.setChecked(extensions.get("autologin", True))

            server_logs = config["server_logs"]
            self.logs_mode.set_current({launch.LOGS_DEFAULT: "Default",
                                        launch.LOGS_ALL: "All",
                                        launch.LOGS_PICK: "Choose"}.get(
                                            server_logs["mode"], "Off"), notify=False)
            self._populate_server_logs(server_logs["names"])

            scenarios = config["scenarios"]
            self._picked = list(scenarios["selected"])
            modes = [mode for mode, _label, _hint in SCENARIO_MODES]
            remembered = scenarios.get("run_mode")
            if remembered in modes[1:]:
                self._run_sub = modes.index(remembered)
            if scenarios["mode"] in modes:
                index = modes.index(scenarios["mode"])
                self.scenario_mode.button(index).setChecked(True)
                if index:
                    self._run_sub = index
            self._recording = bool(config.get("record"))
            self._populate_record_targets(config.get("record_into") or "")
            self.what.set_current(WHAT_LABELS[self._what_value()], notify=False)
            self._refresh_scenarios()

            reports = config["reports"]
            self.report_mode.set_current({launch.REPORTS_FULL: "Full diagnostics",
                                          launch.REPORTS_CUSTOM: "Choose"}.get(
                                             reports["level"], "Results only"),
                                         notify=False)
            self._populate_artifacts(reports["artifacts"])
            self.report_always.setChecked(bool(reports["always"]))
            self.report_doc.setChecked(bool(reports["doc"]))
            index = self.shots.findData(config["screenshots"]["mode"])
            self.shots.setCurrentIndex(index if index >= 0 else 0)

            self.overlay.setChecked(bool(config["overlay"]["enabled"]))
            self._populate_overlay(config["overlay"]["components"])

            advanced = config["advanced"]
            self._own_url = str(advanced["url"] or "").strip()
            self.profile_prefix.setText(advanced["profile_prefix"])
            self.log_level.setCurrentText(advanced["log_level"] or "INFO")
            for key, edit in self.dir_edits.items():
                edit.setText(advanced[key])
        finally:
            self._building = False
        self._changed()

    def _extras(self, config=None):
        """What the rules need from outside this page, as keyword arguments: the
        environment's address, the variables' places, the default folders."""
        config = config if config is not None else self.state()
        return {"env_urls": self.controller.env_urls(),
                "dirs": {"reports": self.settings.directory("reports"),
                         "sessions": self.settings.directory("sessions")},
                "scope": self.controller.scope(config)}

    def _address_extras(self, config):
        extras = self._extras(config)
        return extras["env_urls"], extras["scope"]

    def command_state(self):
        """The Command page's state for this configuration - what History keeps."""
        config = self.state()
        return launch.to_command_state(config, self.inventory, **self._extras(config))

    def argv(self):
        config = self.state()
        return launch.argv(config, self.inventory, **self._extras(config))

    def address(self, config=None):
        """The :data:`launch.Address` the windows will open."""
        config = config if config is not None else self.state()
        return launch.effective_url(config, self.inventory,
                                    *self._address_extras(config))

    def problem_list(self):
        config = self.state()
        return launch.validate(config, self.inventory, *self._address_extras(config))

    def run_meta(self, background=False):
        """One line for the Run page's header."""
        config = self.state()
        return " · ".join([launch.users_label(config, self.inventory),
                           launch.scenarios_label(config),
                           launch.sessions_label(config, background)])

    def describe_line(self, background=False):
        return launch.describe_line(self.state(), self.inventory, background)

    def background_blockers(self):
        """What stops this configuration running with no browser; [] if nothing."""
        config = self.state()
        return launch.background_blockers(
            launch.to_command_state(config, self.inventory)["--run-tests"],
            self.inventory, launch.selected_logins(config, self.inventory))

    def summary_rows(self):
        config = self.state()
        return launch.summarise(config, self.inventory, *self._address_extras(config))

    # -- reactions ------------------------------------------------------------
    def _changed(self, *_args):
        if self._building:
            return
        config = self.state()
        self.users_list.set_locked(config["users"]["mode"] != launch.USERS_PICK)
        self._show_what(config)
        self.ext_list.setEnabled(config["extensions"]["mode"] == launch.EXT_PICK)
        # Fire-and-forget takes the log readers with it when the launcher exits,
        # so the whole block is off rather than quietly ignored later.
        detached = bool(config["sessions"]["detach"])
        self.logs_mode.setEnabled(not detached)
        self.logs_list.setEnabled(not detached
                                  and config["server_logs"]["mode"] == launch.LOGS_PICK)
        self._update_logs_note(detached)
        self.scenario_list.setEnabled(
            config["scenarios"]["mode"] == launch.SCENARIOS_PICK)
        self.report_list.setEnabled(
            config["reports"]["level"] == launch.REPORTS_CUSTOM)
        self.overlay_list.setEnabled(config["overlay"]["enabled"])
        # Greyed out whenever the number is not the thing deciding: left editable
        # under "Auto" it would read as a value in force, which it is not - the
        # governor starts from the machine's core count, not from the box.
        self.jobs.setEnabled(not (config["sessions"]["all_at_once"]
                                  or config["sessions"]["auto_jobs"]))
        self.sessions_button.setText(sessions_label_for(
            config["sessions"]["jobs"], config["sessions"]["all_at_once"],
            config["sessions"]["auto_jobs"]))

        extras = self._extras(config)
        address_extras = (extras["env_urls"], extras["scope"])
        self._show_address(config, extras)
        rows = launch.summarise(config, self.inventory, *address_extras)
        self.summary.setText("   ·   ".join("%s: %s" % (label, value)
                                            for label, value in rows))
        self.context_line.setText(self._context_text(config, extras))
        problems = launch.validate(config, self.inventory, *address_extras)
        self.problems.setText("   ".join(problems))
        # Never gated by the fold: a problem is the reason RUN would refuse, and
        # a refusal with its explanation folded away is the worst of both.
        self.problems.setVisible(bool(problems))
        notes = launch.notes(config, self.inventory, *address_extras)
        self.notes.setText("   ".join(notes))
        expanded = self.summary_toggle.isChecked()
        self.notes.setVisible(bool(notes) and expanded)
        self.run_button.setEnabled(not problems and not self._running())
        self.preview.setText(launch.preview(config, self.inventory, self.core,
                                            **extras))
        self.preview.setVisible(self._developer and expanded)
        self.summary.setVisible(expanded)

        self._update_dirty(config)
        if self.controller.persistent:
            self.settings.save_launch_state(config)
        if self.variables_panel.isVisible():
            self._fill_variables()
        self.state_changed.emit()

    def _running(self):
        return getattr(self, "_is_running", False)

    def _current_env_value(self):
        """The users.json env value the accounts of this environment carry."""
        return self.controller.environment_fields()[1]

    def _template(self, config):
        """The address as written: this configuration's own, else the
        environment's ("" when the environment's origin is all there is)."""
        own = config["advanced"]["url"]
        if own:
            return own
        env = self.controller.environment()
        return env.address if env is not None and not self.controller.is_stand_in(env) \
            else ""

    def _show_address(self, config, extras):
        """The address box, its source tag, Reset, problem and Define, for ``config``."""
        address = launch.effective_url(config, self.inventory, extras["env_urls"],
                                       extras["scope"])
        own = config["advanced"]["url"]
        template = self._template(config)
        self.url_field.set_address(address.parts, template or address.url)
        self.url_field.set_variables(extras["scope"].definitions)
        self.url_field.setToolTip(address.url)
        self.url_field.set_placeholder(
            "type an address, or choose an environment" if not config["environment"]
            else "this environment has no address - type one")
        source = address.source
        if source == launch.SOURCE_CONFIG:
            preset = self.controller.preset()
            saved = (preset.config.get("advanced") or {}).get("url") if preset else None
            if saved != own:
                source = lc.SOURCE_RUN          # written here, not saved yet
        self.url_source.setText(source)
        self.url_source.setVisible(bool(source))
        self.url_reset.setVisible(address.source == launch.SOURCE_CONFIG)
        self.url_problem.setText(address.problem)
        self.url_problem.setVisible(bool(address.problem))
        unknown = [name for name in launchvars.references(template)
                   if name not in extras["scope"].definitions]
        self.url_define.setVisible(bool(unknown))
        if unknown:
            self.url_define.setText("Define %s…" % unknown[0])
            self.url_define.setProperty("variable", unknown[0])

    def _url_edited(self, text):
        """Keep ``text`` as this configuration's own address.

        Committing the address that applies anyway is not an override: typing
        into the box and pressing Enter without changing anything must leave
        the configuration following its environment.
        """
        text = (text or "").strip()
        config = dict(self.state())
        config["advanced"] = dict(config["advanced"], url="")
        inherited_template = self._template(config)
        inherited = launch.effective_url(config, self.inventory,
                                         *self._address_extras(config)).url
        if text and (text == inherited_template
                     or launch.normalize_address(text)[0] == inherited):
            text = ""
        if text == self._own_url:
            self._changed()           # still redraw: the box may show the edit
            return
        self._own_url = text
        self._changed()

    # -- variables ------------------------------------------------------------
    def _open_variables(self, focus=""):
        self._fill_variables()
        self.variables_panel.open_below(self.url_field, focus)

    def _fill_variables(self):
        config = self.state()
        scope = self.controller.scope(config)
        template = self._template(config)
        named = launchvars.references(template)
        rows = []
        everything = named + sorted(name for name in scope.definitions
                                    if name not in named)
        resolved = launchvars.resolve("".join("{{%s}}" % name for name in everything
                                              if name in scope.definitions),
                                      scope.definitions, scope.layers)
        problems = {problem.field: problem.message for problem in resolved.problems}
        for name in everything:
            definition = scope.definitions.get(name)
            if definition is None:
                rows.append({"name": name, "defined": False})
                continue
            value = resolved.values.get(name)
            rows.append({
                "name": name, "defined": True, "type": definition.type,
                "choices": list(definition.choices),
                "text": value.text if value else "",
                "source": value.source if value else "",
                "inherited": value.inherited if value else "",
                "inherited_source": value.inherited_source if value else "",
                "resettable": bool(value) and value.source in (lc.SOURCE_RUN,
                                                               lc.SOURCE_CONFIGURATION),
                "problem": problems.get(name, ""),
            })
        env = self.controller.environment()
        self.variables_panel.set_rows(
            rows, can_set_environment=env is not None
            and not self.controller.is_stand_in(env))

    def _typed(self, name, value):
        """``value`` as ``name``'s type when it is one, else as typed (the
        resolver then says what is wrong with it)."""
        definition = self.controller.definitions().get(name)
        if definition is None or value is None:
            return value
        typed, error = launchvars.coerce(definition, value)
        return value if error else typed

    def _variable_changed(self, name, value):
        """This run's value for ``name``; None resets it.

        Reset takes away the nearest place's say: a value only this run gives
        goes back to what the configuration saved, and one the configuration
        gives is dropped from it, so it inherits the environment's.
        """
        preset = self.controller.preset()
        saved = preset.variables if preset is not None else {}
        if value is None:
            if name in self._run_variables and name in saved \
                    and saved[name] != self._run_variables[name]:
                self._run_variables[name] = saved[name]
            else:
                self._run_variables.pop(name, None)
        else:
            self._run_variables[name] = self._typed(name, value)
        self._changed()
        self._fill_variables()

    def _variable_for_environment(self, name, value):
        try:
            self.controller.set_environment_value(name, self._typed(name, value))
        except lpf.LaunchProjectsError as exc:
            widgets.warn(self, "Variables", "Could not change the environment.",
                         str(exc))
            return
        # The environment says it now; this run no longer has to.
        if self._run_variables.get(name) == self._typed(name, value):
            self._run_variables.pop(name, None)
        self._changed()
        self._fill_variables()

    def _variable_defined(self, name, kind, value, level):
        """Define ``name`` in the project, its value at ``level``."""
        typed = None
        if value not in (None, ""):
            definition = launchvars.Definition(name, kind, None, True, ())
            typed, error = launchvars.coerce(definition, value)
            if error:
                widgets.warn(self, "Variables", "%s %s." % (name, error))
                return
        try:
            self.controller.define(name, kind, typed if level == "project" else None)
            if level == "environment" and typed is not None:
                self.controller.set_environment_value(name, typed)
        except lpf.LaunchProjectsError as exc:
            widgets.warn(self, "Variables", "Could not define %s." % name, str(exc))
            return
        if level == "run" and typed is not None:
            self._run_variables[name] = typed
        self._changed()
        self._fill_variables()

    def _context_text(self, config, extras):
        """"Project / Environment · where it opens · how many accounts"."""
        env = self.controller.environment()
        project = self.controller.project(env)
        address = launch.effective_url(config, self.inventory, extras["env_urls"],
                                       extras["scope"]).url
        where = address.split("://", 1)[-1].rstrip("/") if address else "no address"
        logins = launch.selected_logins(config, self.inventory)
        parts = ["%s / %s" % (project.name if project is not None else "?",
                              env.name if env is not None else "no environment"),
                 where, "%d account%s" % (len(logins), "" if len(logins) == 1 else "s")]
        return "   ·   ".join(parts)

    def _add_account(self):
        """+ Account: a short form for this environment; the account it saves is
        chosen for this run."""
        env = self.controller.environment()
        if env is None or self.controller.is_stand_in(env):
            widgets.note(self, "Add an account",
                         "Choose an environment first: an account belongs to one.")
            return
        from .launchaccountform import AccountForm
        form = AccountForm(self, env, self._users_path(), self.inventory)
        if form.exec() != QDialog.Accepted or not form.saved_login:
            return
        login = form.saved_login
        if self.users_mode.current() == "Choose accounts":
            self._pending_tick = login
        self.accounts_changed.emit()

    def _users_path(self):
        return self.users_path or (
            self.inventory.config_path if self.inventory is not None else "")

    # -- moving around --------------------------------------------------------
    def _project_picked(self, index):
        project_id = self.project_combo.itemData(index)
        if project_id is None or project_id == self.controller.project_id:
            return
        self._open(*self.controller.select_project(project_id, self.state()))

    def _reload_projects(self):
        self.project_combo.blockSignals(True)
        self.project_combo.clear()
        current = 0
        for index, (project_id, name) in enumerate(self.controller.projects()):
            self.project_combo.addItem(name, project_id)
            if project_id == self.controller.project_id:
                current = index
        if self.project_combo.count():
            self.project_combo.setCurrentIndex(current)
        self.project_combo.blockSignals(False)

    def _open(self, state, baseline):
        """Show ``state``, measured against ``baseline`` (None: a new one)."""
        self._baseline = baseline
        self._reload_projects()
        self._reload_environments()
        self._reload_configs()
        self.set_state(state)
        if baseline is None:
            self._mark_clean()

    def _environment_picked(self, index):
        env_id = self.env_combo.itemData(index)
        if env_id is None or env_id == self.controller.environment_id:
            return
        self._open(*self.controller.select_environment(env_id, self.state()))

    def _environment_changed(self, _text=None):
        # Kept for callers that set the combo directly: the same as picking.
        self._environment_picked(self.env_combo.currentIndex())

    def _users_changed(self):
        if not self._building:
            self._refresh_scenarios()
        self._changed()

    def _users_mode_changed(self):
        choosing = self.users_mode.current() == "Choose accounts"
        self.users_list.set_locked(not choosing)
        if not self._building and choosing and not self.users_list.checked():
            self.users_list.set_all(True)     # "choose" starts from everything
        if not self._building:
            self._refresh_scenarios()
        self._changed()

    def _ext_mode_changed(self):
        if not self._building and self.ext_mode.current() == "Choose" \
                and not self.ext_list.checked():
            self.ext_list.set_all(True)
        self._changed()

    def _report_mode_changed(self):
        if not self._building and self.report_mode.current() == "Choose" \
                and not self.report_list.checked():
            self.report_list.set_checked(["result"])
        self._changed()

    def _scenario_mode_changed(self, index, checked):
        if checked:
            if index:
                # A scenario mode picked is Run scenarios, however it was picked.
                self._run_sub = index
                self._recording = False
            if not self._building:
                self.what.set_current(WHAT_LABELS[self._what_value()], notify=False)
                self._refresh_scenarios()
            self._changed()

    def _what_value(self):
        """Open windows, Run scenarios or Record, as the radio group and the
        record flag say - the segmented control only shows it."""
        if self._recording:
            return WHAT_RECORD
        return WHAT_RUN if self._scenario_mode_id() else WHAT_OPEN

    def _what_changed(self):
        """Open windows / Run scenarios / Record picked.

        Each keeps the others' answers: which scenarios Run means is remembered
        while the windows are only opened, or recorded in.
        """
        label = self.what.current()
        what = next(key for key, text in WHAT_LABELS.items() if text == label)
        self._recording = what == WHAT_RECORD
        index = self._run_sub if what == WHAT_RUN else 0
        self.scenario_mode.button(index).setChecked(True)
        if not self._building:
            self._refresh_scenarios()
        self._changed()

    def _populate_record_targets(self, keep):
        """A new scenario, then every scenario of one's own; ``keep`` chosen.

        Only writable ones: a scenario shipped with the application cannot be
        recorded into. One the configuration names that is gone is kept and
        marked, as everywhere on this page, rather than quietly dropped.
        """
        self.record_into.blockSignals(True)
        self.record_into.clear()
        self.record_into.addItem("A new scenario", "")
        known = set()
        for row in (self.inventory.scenarios if self.inventory else []):
            if row.get("writable") and row.get("id"):
                known.add(row["id"])
                name = row.get("name") or ""
                self.record_into.addItem("%s   %s" % (row["id"], name) if name
                                         else row["id"], row["id"])
        if keep and keep not in known:
            self.record_into.addItem("%s (not found)" % keep, keep)
        index = self.record_into.findData(keep)
        self.record_into.setCurrentIndex(index if index >= 0 else 0)
        self.record_into.blockSignals(False)

    def record_target(self):
        """The scenario RUN records into ("" for a new one)."""
        return self.record_into.currentData() or ""

    def recording(self):
        """Whether RUN means "open the windows with the recorder"."""
        return self._what_value() == WHAT_RECORD

    def _show_what(self, config):
        """Only what the chosen answer uses is on screen; the rest keeps its
        values, out of sight, for when it is chosen again."""
        what = self._what_value()
        self.what_hint.setText(WHAT_HINTS[what])
        self.what_hint.setVisible(bool(WHAT_HINTS[what]))
        running = what == WHAT_RUN
        self.scenario_row.setVisible(running)
        self.scenario_list.setVisible(running)
        self.keep_open.setVisible(running)
        self.sessions_box.setVisible(what != WHAT_RECORD)
        self.load_warning.setVisible(what != WHAT_RECORD
                                     and bool(self.load_warning.text()))
        self.record_box.setVisible(what == WHAT_RECORD)
        # Kept in place, not hidden: hiding it moved everything under it, and
        # the page's scroll with it, each time the answer changed.
        self.reports_box.setEnabled(running)
        self.reports_note.setVisible(not running)

    def _scenario_list_changed(self):
        # Only a tick made under "Choose scenarios" is a pick; the list is
        # read-only in every other mode.
        if self._scenario_mode_value() == launch.SCENARIOS_PICK:
            self._picked = self.scenario_list.checked()
        self._changed()

    def _summary_toggled(self, expanded):
        """Fold the footer's prose away, leaving the buttons and any problem.

        The visibility itself is settled in :meth:`_changed`, which already knows
        which of these have anything to say - two writers would fight over it and
        the next edit would quietly unfold what was just folded.
        """
        icons.button(self.summary_toggle,
                     "disclosure_open" if expanded else "disclosure_closed",
                     "SUMMARY")
        if self._building:
            return          # _restore() settles the footer once, at the end
        self.settings.launch_summary_expanded = expanded
        self._changed()

    def _jobs_all_toggled(self, checked):
        if checked and not self._building:
            self.jobs_auto.setChecked(False)   # a fixed all-at-once is not auto
        self._changed()

    def _jobs_auto_toggled(self, checked):
        if checked and not self._building:
            self.jobs_all.setChecked(False)
        self._changed()

    def _overlay_toggled(self, checked):
        if not self._building and checked and not self.overlay_list.checked():
            self.overlay_list.set_all(True)
        self._changed()

    # -- reading the controls -------------------------------------------------
    def _ext_mode_value(self):
        return {"None": launch.EXT_NONE, "Choose": launch.EXT_PICK}.get(
            self.ext_mode.current(), launch.EXT_ALL)

    def _report_mode_value(self):
        return {"Full diagnostics": launch.REPORTS_FULL,
                "Choose": launch.REPORTS_CUSTOM}.get(self.report_mode.current(),
                                                     launch.REPORTS_RESULTS)

    def _scenario_mode_id(self):
        checked = self.scenario_mode.checkedId()
        return checked if checked >= 0 else 0

    def _scenario_mode_value(self):
        return SCENARIO_MODES[self._scenario_mode_id()][0]

    # -- context --------------------------------------------------------------
    def set_core(self, core):
        self.core = core
        self._changed()

    def set_inventory(self, inventory):
        """Repopulate every list from what the core says exists."""
        self.inventory = inventory
        config = self.state()
        # What the inventory says decides which environment the open
        # configuration belongs to, and which configurations wait for one.
        self.controller.set_inventory(inventory)
        self._building = True
        try:
            self._reload_projects()
            self._reload_environments()
            self._reload_configs()
            self._populate_users(config["users"]["logins"])
            self._populate_extensions(config["extensions"]["names"])
            self._populate_server_logs(config["server_logs"]["names"])
            self._picked = list(config["scenarios"]["selected"])
            self._refresh_scenarios()
            self._populate_record_targets(config.get("record_into") or "")
            self._populate_artifacts(config["reports"]["artifacts"])
            self._populate_overlay(config["overlay"]["components"])

            for key, name in (("reports_dir", "reports"),
                              ("sessions_dir", "sessions")):
                # The Environments page owns the defaults. Shown, not copied in:
                # copied, they became part of every configuration saved after,
                # and stopped following that page when it changed.
                self.dir_edits[key].setPlaceholderText(
                    self.settings.directory(name) or inventory.dirs().get(name)
                    or "(default)")
        finally:
            self._building = False
        self._update_env_note()
        self._changed()

    def refresh_address(self):
        """An environment changed on the Environments page."""
        self.controller.registry.reload()
        self._lists_changed()
        self._changed()

    def set_developer_mode(self, enabled):
        self._developer = bool(enabled)
        self.preview.setVisible(self._developer)

    def set_running(self, running):
        self._is_running = bool(running)
        self.run_button.setEnabled(not running and not self.problem_list())

    # -- populating -----------------------------------------------------------
    def _populate_users(self, keep):
        """Accounts for the chosen environment, with ``keep`` still ticked.

        A login in ``keep`` that the inventory no longer offers is added anyway,
        marked as missing: dropping it silently would quietly change a saved
        configuration the moment it was opened.
        """
        env_value = self._env_filter()
        rows = []
        if self.inventory:
            for user in self.inventory.users:
                if env_value is not None and user.get("env") != env_value:
                    continue
                role = user.get("class", "")
                if env_value is None:
                    role = "%s · %s" % (role or "?", user.get("env", ""))
                rows.append({"login": user.get("login", ""), "role": role,
                             "scenarios": list(user.get("tests") or ())})
        known = {row["login"] for row in rows}
        if self._pending_tick and self._pending_tick in known:
            keep = list(keep or ()) + [self._pending_tick]
            self._pending_tick = ""
        for login in keep or ():
            if login not in known:
                rows.append({"login": login, "role": "", "scenarios": [],
                             "missing": True})
        self.users_list.set_rows(rows, keep)
        self._update_env_note()

    def _populate_extensions(self, keep):
        self.ext_list.clear()
        seen = set()
        for extension in (self.inventory.extensions if self.inventory else []):
            name = extension.get("name", "")
            if not name or name in seen:
                continue       # the same name can arrive twice (local and store)
            seen.add(name)
            usable = bool(extension.get("usable", True))
            note = extension.get("kind", "")
            if not usable:
                note = "unavailable: %s" % (extension.get("reason") or "unknown")
            self.ext_list.add(name, name, note, enabled=usable)
        for name in keep or ():
            if name not in seen:
                self.ext_list.add(name, name, "(not installed)")
                seen.add(name)
        self.ext_list.set_checked(keep)
        _fit_rows(self.ext_list)

    def _populate_scenarios(self, keep):
        self.scenario_list.clear()
        known = set()
        for tag in (self.inventory.tags if self.inventory else []):
            value = "tag:%s" % tag
            known.add(value)
            self.scenario_list.add(value, value, "every scenario with this tag",
                                   accent=True)
        for scenario in (self.inventory.scenarios if self.inventory else []):
            value = scenario.get("id", "")
            if not value:
                continue
            known.add(value)
            note = scenario.get("name") or ""
            if not scenario.get("in_all", True):
                note += "   (not part of \"every scenario\")"
            self.scenario_list.add(value, value, note)
        for value in keep or ():
            if value not in known:
                self.scenario_list.add(value, value, "(not found)")
                known.add(value)
        self.scenario_list.set_checked(keep)

    def _refresh_scenarios(self):
        """Fill the scenario list for the mode it is answering.

        Under "each account's own list" it shows what the accounts this run would
        launch carry on the Credentials page, ticked and narrowed to Selected - the
        list is read-only there, so it is a view of those lists and nothing else.
        Every other mode shows the pick, which is kept aside so that looking at the
        accounts' lists never rewrites it.
        """
        view = self.scenario_list.view
        if self._scenario_mode_value() == launch.SCENARIOS_PER_USER:
            self._populate_scenarios(
                launch.account_scenarios(self.state(), self.inventory))
            self.scenario_list.set_nothing_selected(
                "None of the chosen accounts has scenarios set on the "
                "Credentials page.")
            if self._view_before is None:
                self._view_before = view.current()
            view.set_current(widgets.VIEW_SELECTED)
            return
        self._populate_scenarios(self._picked)
        self.scenario_list.set_nothing_selected()
        if self._view_before is not None:
            view.set_current(self._view_before)
            self._view_before = None

    def _populate_artifacts(self, keep):
        self.report_list.clear()
        choices = (self.inventory.choices("report_artifacts", launch.ALL_ARTIFACTS)
                   if self.inventory else launch.ALL_ARTIFACTS)
        notes = {"console": "browser console output", "dom": "the page's HTML",
                 "result": "pass/fail detail", "screen": "screenshots",
                 "url": "the address each step ended on"}
        for choice in choices:
            self.report_list.add(choice, choice, notes.get(choice, ""))
        self.report_list.set_checked(keep)
        _fit_rows(self.report_list)

    def _populate_overlay(self, keep):
        self.overlay_list.clear()
        choices = (self.inventory.choices("overlay_components", launch.ALL_OVERLAY)
                   if self.inventory else launch.ALL_OVERLAY)
        for choice in choices:
            self.overlay_list.add(choice, choice)
        self.overlay_list.set_checked(keep)
        _fit_rows(self.overlay_list)

    def _env_filter(self):
        """The env value accounts and logs are filtered by; None for every one."""
        env = self.controller.environment()
        if env is None or env.id == lc.ALL_ENVIRONMENTS:
            return None
        return env.account_key

    def _env_label(self, env):
        """How an environment reads in the combo."""
        if env.id == lc.ALL_ENVIRONMENTS:
            return "All environments (one address for everyone)"
        name = env.name
        if self.controller.is_stand_in(env):
            name = "%s (missing)" % name
        return name

    def _reload_environments(self):
        """Fill the environment combo from the controller, the current one chosen."""
        self.env_combo.blockSignals(True)
        self.env_combo.clear()
        current = 0
        for index, env in enumerate(self.controller.environments(
                self.controller.project_id)):
            self.env_combo.addItem(self._env_label(env), env.id)
            if env.id == self.controller.environment_id:
                current = index
        if self.env_combo.count():
            self.env_combo.setCurrentIndex(current)
        self.env_combo.blockSignals(False)

    def _lists_changed(self):
        """The controller's projects, environments or configurations changed."""
        self._reload_projects()
        self._reload_environments()
        self._reload_configs()

    def _update_env_note(self):
        env = self.controller.environment()
        self.env_move.setVisible(env is not None and self.controller.is_stand_in(env)
                                 and self.controller.preset() is not None)
        if self.controller.registry.error:
            # Said where the environments are chosen: everything still runs,
            # from users.json alone, but nothing set up is in force and
            # nothing set here can be kept until the file reads again.
            self.env_note.setText("launch-projects.json cannot be read, so its "
                                  "addresses and variables are not in force: %s"
                                  % self.controller.registry.error.splitlines()[0])
            self.env_note.setToolTip(self.controller.registry.error)
            return
        if not self.inventory:
            self.env_note.setText("")
            return
        if env is None or env.id == lc.ALL_ENVIRONMENTS:
            self.env_note.setText("%d environments · %d accounts"
                                  % (len(self.inventory.envs),
                                     len(self.inventory.users)))
            return
        value = self._current_env_value()
        for row in self.inventory.envs:
            if row.get("value") == value:
                # Not the address: that is the box below, which may differ.
                self.env_note.setText("%s accounts · filed under %s"
                                      % (row.get("count", "?"), value))
                return
        if not self.controller.is_stand_in(env):
            self.env_note.setText("No accounts filed under %s yet" % (value or "?"))
            return
        # Kept, because it is what the configuration says (see launchcontroller),
        # but users.json no longer has it - so say so rather than leave a blank
        # line under an environment that would launch nothing.
        self.env_note.setText("Not in users.json any more · 0 accounts")

    # -- unsaved changes ------------------------------------------------------
    # A saved configuration is a file, and the controls are a working copy of it.
    # Nothing writes back on its own - a run does not save - so the page has to
    # say when the two have parted: both Save buttons go red, and say so in
    # words, the moment the working copy stops matching what was opened, and go
    # quiet again the moment it is written or reverted.
    #
    # Switching away does not drop the edit either. Each configuration keeps its
    # own working copy (launch-drafts.json) for as long as it differs from its
    # file, and opening it again brings the edit back - so moving between
    # configurations never needs a "discard your changes?" question.
    #
    # "(unsaved)" is the same story with no file at the end of it: the settings
    # being built there are not stored anywhere, so an edit is flagged just the
    # same, and Save - which asks for a name - is the thing that settles it. The
    # baseline is taken when the page arrives at (unsaved) rather than being the
    # defaults, so opening the app to yesterday's restored settings is quiet and
    # only what happens next is flagged.

    def _comparable(self, config):
        """A configuration reduced to what "the same settings" means.

        Compared as text, through :func:`launch.merged`, so a file written before
        a key existed does not read as an edit; the lists are sorted because the
        order the tick boxes happen to be in is not part of the configuration -
        the inventory arriving can reorder them without anything having changed.
        The environment is not compared at all: it is where the page is, not
        part of what a configuration says - going to another one opens that
        one's configurations, and the inventory arriving to say which
        environment the open one belongs to is not an edit either.
        """
        config = launch.merged(config)
        config.pop("environment", None)
        config.pop("environment_value", None)

        def norm(value):
            if isinstance(value, dict):
                return {key: norm(value[key]) for key in sorted(value)}
            if isinstance(value, list):
                return sorted(str(item) for item in value)
            return value

        return json.dumps(norm(config), sort_keys=True)

    def is_dirty(self):
        """Whether the controls have moved away from the saved configuration."""
        return self._dirty

    def _mark_clean(self, config=None):
        """What is on screen now is the thing to measure later edits against."""
        self._baseline = json.loads(json.dumps(config if config is not None
                                               else self.state()))
        self._update_dirty()

    def _current_name(self):
        """The open configuration's name; "" for (unsaved)."""
        preset = self.controller.preset()
        return preset.name if preset is not None else ""

    def _current_config_name(self):
        return self._current_name()

    def _update_dirty(self, config=None):
        name = self._current_name()
        config = config if config is not None else self.state()
        # The baseline is kept as a document and compared afresh each time: the
        # environment's value is only known once the inventory has arrived, and
        # a baseline frozen as text before that would read as an edit after.
        dirty = self._baseline is not None and \
            self._comparable(config) != self._comparable(self._baseline)
        self._dirty = dirty
        if not self._building:
            self.controller.keep_draft(config, dirty, self._comparable(config))
        self.unsaved_note.setVisible(dirty)
        self.reset_button.setText("Revert" if name else "Reset")
        self.reset_button.setToolTip(
            "Put back the saved \"%s\" and drop the changes" % name if name else
            "Start these settings over from the defaults")
        tip = ("Unsaved changes to \"%s\"" % name if name else
               "These settings are not saved as a configuration")
        for button in (self.footer_save,):
            button.setProperty("dirty", "true" if dirty else "false")
            button.setToolTip(tip if dirty else "")
            # A stylesheet rule keyed on a property is only re-read when the
            # widget is re-polished; without this the button keeps the colour it
            # was built with.
            button.style().unpolish(button)
            button.style().polish(button)
        # A desktop link points at a saved configuration by its id, so there has
        # to be one, and it has to match what is on screen. A checkable menu
        # entry draws its own tick, so whether a link exists needs no wording.
        preset_id = self.controller.preset_id
        is_saved = not dirty and bool(preset_id)
        self.desktop_link.setEnabled(is_saved)
        self.desktop_link.setChecked(
            is_saved and os.path.exists(self._desktop_link_path(preset_id)))
        env = self.controller.environment()
        self.use_elsewhere.setEnabled(bool(preset_id) and env is not None)

    def set_persistent(self, persistent):
        """Whether edits are remembered for the next start (False: a desktop link).

        A run started from a desktop link is a separate, windowless copy of the
        application. Letting it write the working copy and the opened
        configuration would make the next normal start open whatever the link
        ran instead of what was left on screen.
        """
        self.controller.persistent = bool(persistent)

    # -- saved configurations -------------------------------------------------
    def _reload_configs(self):
        """The configurations of this environment, the open one chosen."""
        self.config_combo.blockSignals(True)
        self.config_combo.clear()
        self.config_combo.addItem(UNSAVED, lc.NEW)
        current = 0
        for index, preset in enumerate(self.controller.presets_here(), start=1):
            self.config_combo.addItem(preset.name, preset.id)
            if preset.id == self.controller.preset_id:
                current = index
        self.config_combo.setCurrentIndex(current)
        self.config_combo.blockSignals(False)

    def _config_picked(self, index):
        preset_id = self.config_combo.itemData(index) or lc.NEW
        if preset_id == self.controller.preset_id and preset_id:
            return
        self._open(*self.controller.select_preset(preset_id, self.state()))

    def open_configuration(self, token, use_draft=True):
        """Open the saved configuration ``token`` names (an id, or a name);
        False when none does - or when the name belongs to several.

        ``use_draft=False`` opens it as saved, ignoring an unsaved edit - what a
        desktop link runs, since a link means the configuration as saved.
        """
        if use_draft:
            preset, _candidates = self.controller.presets.find(token)
            if preset is None:
                return False
            self._open(*self.controller.select_preset(preset.id, self.state()))
            return True
        opened, _candidates = self.controller.open_saved(token)
        if opened is None:
            return False
        self._open(*opened)
        return True

    def link_candidates(self, token):
        """The configurations a shared name could mean, for the error it gets."""
        return [preset.name for preset in self.controller.presets.find(token)[1]]

    def open_unsaved(self, config):
        """Put ``config`` on screen as new - a run brought back from History.

        In the environment it launched, never into the configuration that
        happens to be open: that made it look like an edit of it, and Save then
        wrote a past run over the file.
        """
        self._open(self.controller.open_unsaved(config), None)

    def _saved(self, preset):
        self._baseline = self.controller.compose(preset)
        self._reload_configs()
        self._changed()

    def save_configuration(self):
        state = self.state()
        name = None
        if self.controller.preset() is None:
            name = widgets.ask_text(self, "Save configuration",
                                    "Name this configuration:")
            if not name:
                return
        try:
            preset = self.controller.save(state, name)
        except lps.PresetConflict as conflict:
            answer = widgets.choose(
                self, "Save configuration",
                "\"%s\" was saved elsewhere since it was opened here."
                % conflict.current.name,
                "Replacing it throws that save away; a copy keeps both.",
                choices=("Replace it",), agree="Save as a copy", refuse="Cancel",
                kind="warn")
            if answer == "Replace it":
                preset = self.controller.save(state, overwrite=True)
            elif answer == "Save as a copy":
                preset = self.controller.save_as(state, conflict.current.name)
            else:
                return
        self._saved(preset)

    def duplicate_configuration(self):
        """Save as: a new configuration of this environment from what is on screen."""
        base = self._current_name() or "Launch configuration"
        name = widgets.ask_text(self, "Save as", "Name the new configuration:",
                                self.controller.presets.unique_name(
                                    (self.controller.environment()
                                     or lpf.Environment("", lpf.WORKSPACE, "", "", "",
                                                        {}, "", 0, True)).project_id,
                                    base))
        if not name:
            return
        self._saved(self.controller.save_as(self.state(), name))

    def rename_configuration(self):
        old = self._current_name()
        if not old:
            widgets.note(self, "Rename", "Save this configuration first.")
            return
        new = widgets.ask_text(self, "Rename configuration", "New name:", old)
        if not new or new == old:
            return
        # The id does not change, so desktop links and History still find it;
        # a name already taken in the project becomes "name (2)".
        self.controller.rename(new)
        self._reload_configs()
        # The settings did not move, only the name they are filed under: an edit
        # that was unsaved before the rename is still unsaved after it.
        self._update_dirty()

    def use_with_another_environment(self):
        """Put the open configuration in another environment.

        A copy, so the original keeps running where it was - except for one
        whose environment is gone, which is moved: there is nothing left to
        keep it for.
        """
        current = self.controller.environment()
        targets = [env for env in self.controller.environments()
                   if not self.controller.is_stand_in(env)
                   and (current is None or env.id != current.id)]
        if not targets or self.controller.preset() is None:
            return
        move = current is not None and self.controller.is_stand_in(current)
        chosen = widgets.ask_item(
            self, "Use with another environment",
            "Move \"%s\" to:" % self._current_name() if move else
            "Copy \"%s\" to:" % self._current_name(),
            [self._env_label(env) for env in targets],
            detail=("Its environment is gone, so it moves." if move else
                    "The original stays where it is."))
        if chosen is None:
            return
        target = targets[[self._env_label(env) for env in targets].index(chosen)]
        self.controller.use_with_environment(target.id, self.state(), move=move)
        self._open(*self.controller.opening(self.state()))

    def _desktop_link_path(self, preset_id):
        links = self.settings.desktop_links()
        if preset_id in links and "path" in links[preset_id]:
            return links[preset_id]["path"]
        preset = self.controller.presets.get(preset_id)
        safe_name = (preset.name if preset else preset_id).replace(" ", "_") \
            .replace("/", "_")
        return os.path.expanduser(f"~/Desktop/QAVector_{safe_name}.desktop")

    @staticmethod
    def _exec_arg(text):
        """``text`` as one quoted argument of a .desktop Exec line.

        The Desktop Entry rules: double quotes around it, a backslash before
        each of " ` $ \\ inside, and a literal % written twice.
        """
        escaped = "".join("\\" + char if char in '"`$\\' else char for char in text)
        return '"%s"' % escaped.replace("%", "%%")

    def _toggle_desktop_link(self):
        preset = self.controller.preset()
        if preset is None:
            return
        name, preset_id = preset.name, preset.id
        links = self.settings.desktop_links()
        current = links.get(preset_id, {})

        if self.desktop_link.isChecked():
            safe_name = name.replace(" ", "_").replace("/", "_")
            default_path = os.path.expanduser("~/Desktop")
            default_filename = f"QAVector_{safe_name}.desktop"

            saved_dir = current.get("path")
            if saved_dir and os.path.isdir(os.path.dirname(saved_dir)):
                initial_path = os.path.dirname(saved_dir)
            else:
                initial_path = default_path

            dlg = DesktopLinkDialog(self,
                                    current.get("name", f"QAVector: {name}"),
                                    initial_path,
                                    current.get("icon", "google-chrome"))
            if dlg.exec() != QDialog.Accepted:
                self.desktop_link.setChecked(False)
                return

            custom_name = dlg.name_edit.text().strip() or f"QAVector: {name}"
            custom_dir = dlg.path_edit.text().strip() or default_path
            custom_icon = dlg.icon_edit.text().strip() or "google-chrome"

            path = os.path.join(custom_dir, default_filename)

            import sys
            bootstrap_py = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "bootstrap.py"))
            # By id: a rename leaves the link pointing where it did.
            content = "\n".join((
                "[Desktop Entry]", "Version=1.0", "Type=Application",
                "Name=%s" % custom_name,
                "Exec=%s %s --launch %s" % (self._exec_arg(sys.executable),
                                            self._exec_arg(bootstrap_py),
                                            self._exec_arg(preset_id)),
                "Terminal=false", "Icon=%s" % custom_icon, ""))
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            os.chmod(path, 0o755)

            import subprocess
            import shutil
            if shutil.which("gio"):
                subprocess.run(["gio", "set", path, "metadata::trusted", "true"], capture_output=True)

            links[preset_id] = {"path": path, "name": custom_name, "icon": custom_icon}
            self.settings.save_desktop_links(links)
            self._update_dirty()
        else:
            path = current.get("path")
            if not path:
                path = self._desktop_link_path(preset_id)
            if path and os.path.exists(path):
                if not widgets.confirm(
                        self, "Remove Desktop Link",
                        "Remove the desktop link?", agree="Remove",
                        kind="warn"):
                    self.desktop_link.setChecked(True)
                    return
                os.remove(path)
            if preset_id in links:
                del links[preset_id]
                self.settings.save_desktop_links(links)
            self._update_dirty()

    def delete_configuration(self):
        preset = self.controller.preset()
        if preset is None:
            return
        if not widgets.confirm(
                self, "Delete configuration", "Delete \"%s\"?" % preset.name,
                "The current settings stay as they are.",
                agree="Delete", kind="warn"):
            return

        path = self._desktop_link_path(preset.id)
        if os.path.exists(path):
            os.remove(path)

        links = self.settings.desktop_links()
        if preset.id in links:
            del links[preset.id]
            self.settings.save_desktop_links(links)

        state = self.state()
        self.controller.delete()
        self._reload_configs()
        self._mark_clean(state)

    # -- search (Ctrl+K) -------------------------------------------------------
    def search_rows(self):
        """Every saved configuration, said the way History says a run."""
        found = []
        envs = {env.id: env for env in self.controller.environments()}
        for preset in self.controller.presets.all():
            env = envs.get(self.controller.preset_environment_id(preset))
            config = self.controller.compose(preset, env)
            found.append(search.hit(preset.name,
                                    launch.describe_line(config, self.inventory),
                                    target=preset.id))
        return sorted(found, key=lambda hit: hit.title.lower())

    def show_found(self, preset_id):
        """Open the configuration as choosing it in the combo would.

        Nothing to ask first: an unsaved edit stays with its own configuration
        and is there again when that one is opened.
        """
        if self.controller.presets.get(preset_id) is None:
            return False
        if preset_id == self.controller.preset_id:
            return True
        self._open(*self.controller.select_preset(preset_id, self.state()))
        return True

    # -- helpers --------------------------------------------------------------
    def reset(self):
        """Revert a saved configuration to what it saved; start a new one over."""
        saved = self.controller.revert()
        if saved is not None:
            self._baseline = saved
            self.set_state(saved)
            return
        fresh = launch.merged(launch.DEFAULTS)
        self.set_state(fresh)
        self._mark_clean()

    def _browse(self, edit):
        start = edit.text() or os.path.expanduser("~")
        chosen = QFileDialog.getExistingDirectory(self, "Choose a folder", start)
        if chosen:
            edit.setText(chosen)

    def _restore(self):
        # What the context says is open - its working copy, else its saved file,
        # else, for a new one, the settings last left on screen. A saved one is
        # measured against its file, so an edit left unsaved when the window
        # closed is still an unsaved edit when it comes back.
        state, baseline = self.controller.opening(
            self.settings.launch_state() or launch.DEFAULTS)
        self._baseline = baseline
        self._reload_projects()
        self._reload_environments()
        self._reload_configs()
        self.set_state(state)
        if baseline is None:
            self._mark_clean()


def _fit_rows(checklist, most=8):
    """Make a short fixed list as tall as its rows (``most`` at the very most).

    Extensions, report artifacts, overlay parts: a handful each, sitting in
    Advanced. Left to stretch, each was a tall box with a few rows at the top.
    """
    view = checklist.list
    rows = min(most, max(1, view.count()))
    row_height = view.sizeHintForRow(0) if view.count() else 20
    view.setFixedHeight(rows * row_height + 2 * view.frameWidth() + 6)
