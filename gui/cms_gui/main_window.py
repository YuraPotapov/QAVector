"""The main window: toolbar, CONFIGURE/OBSERVE sidebar, pages, status bar.

Layout follows the design export: a run/stop toolbar across the top, a narrow
navigation rail split into what you *configure* and what you *observe*, and a
status bar that always says which core, interpreter and config are in play - the
three things that decide what any button here will actually do.

Two pages can start a run - Launch Sessions for a user, Command for a developer -
so this window is also the place that decides which of them the toolbar's RUN
acts on, and the place that records the result. Both concerns live here rather
than in either page, so neither page has to know the other exists.
"""

import logging
import os

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (QApplication, QDialog, QFrame, QInputDialog, QLabel,
                               QMainWindow, QMenu,
                               QPushButton, QSizePolicy, QStackedWidget,
                               QToolButton, QVBoxLayout, QWidget)

from . import (commands, core as core_mod, cyclereplay,
               history as history_mod, icon, icons,
               launch as launch_mod, load as load_mod,
               logsourcesfile as lsf, theme, titlebar, widgets)
from .askbridge import AskBridge
from .loader import LoaderThread
from . import version as gui_version
from .runner import LauncherProcess, RunState
from .servicebridge import ServiceBridge
from .settings import Settings
from .pages.artifacts import ArtifactsPage
from .pages.command import CommandPage
from .pages.credentials import CredentialsPage
from .pages.cycles import CyclesPage
from .pages.services import ServicesPage
from .pages.environments import EnvironmentsPage
from .pages.history import HistoryPage
from .pages.launch import LaunchSessionsPage
from .pages.log import LogPage
from .pages.run import RunPage
from .pages.scenarios import ScenariosPage
from .pages.settings_dialog import SettingsDialog

# (page key, label, icon name painted by icons.icon)
CONFIGURE = [("environments", "Environments", "environments"),
             ("credentials", "Credentials", "credentials"),
             ("logsources", "Services & Logs", "services"),
             ("scenarios", "Scenarios", "scenarios"),
             ("cycles", "Cycles", "cycles"),
             ("commands", "Command", "command"),
             ("launch", "Launch Sessions", "launch")]
OBSERVE = [("run", "Run", "run"), ("log", "Log", "log"),
           ("artifacts", "Artifacts", "artifacts"),
           ("history", "History", "history")]

# Pages only a developer needs. Launch Sessions is never in here: it is the
# primary interface in one mode and still available in the other.
DEVELOPER_ONLY = ("commands",)

# How many live counts a rail entry can show: Services & Logs says how many are
# running and how many have failed, Run says how many windows are up. Every
# entry reserves room for the larger of these whether or not it uses it, so the
# points line up in one column and the marks stay aligned when the rail is
# collapsed - see widgets.NavButton.
NAV_BADGES = {"logsources": 2, "run": 1}

# The rail's width is measured from its contents (see _build_ui); these only set
# the floor, the left/right inset of the group headings, and enough slack that a
# label never sits flush against the divider.
RAIL_MIN_WIDTH = 190
RAIL_PADDING = 14
RAIL_SLACK = 12

# Collapsed, the rail is measured the same way - from an icon-only button, whose
# width comes from the style and the screen's pixel ratio rather than from
# anything written here. This is only the floor, and the room the handle needs.
RAIL_COLLAPSED_MIN_WIDTH = 48

# Taken off a group heading's measured width. "CONFIGURE" is letter-spaced, and the
# 2px lands after the final character as well as between characters, so its
# reported width ends in space that draws nothing. A heading should not widen the
# rail on account of that.
HEADING_TRIM = 10

# How long to wait for the launcher to stop when the window is closed on top of a
# live run. The launcher SIGTERMs every window and gives Chrome up to 15 s each to
# flush its cookies, so anything shorter risks closing on a login mid-write.
CLOSE_WAIT_MS = 20000

# The splash. READY is how long "Ready!" stays up once the pages are populated -
# long enough to read rather than a flicker. MAX is the hard stop: --describe is
# a subprocess that can hang (a busy machine, a core that never answers), and a
# splash with no window behind it is an app that looks dead. It always goes.
SPLASH_READY_MS = 800
SPLASH_MAX_MS = 15000

# How many running services the footer names before it starts counting instead.
# The status bar shares its line with the machine's load and the worker limit,
# so a stack of eight would push both off the end.
SERVICES_IN_FOOTER = 3


def _run_label(text):
    """RUN. The menu's arrow is drawn by Qt in the button's own arrow half."""
    return text


def _run_icon():
    """The play mark, in the ink a primary button paints its label in."""
    return icons.icon("run", theme.BG)


def _close_warning(windows):
    """What the close-during-a-run warning says, for ``windows`` open windows.

    Its own function so the wording can be tested without standing up a dialog.
    """
    closes = (" and closes %d window%s" % (windows, "" if windows == 1 else "s")
              if windows else "")
    return ("Closing stops the run first%s, so each window shuts down gracefully "
            "and keeps its login. This can take a few seconds." % closes)


def _background_tip(blockers):
    """The In Background entry's tooltip: what it does, or what stands in its way."""
    if not blockers:
        return ("Run with no browser at all: the service steps and their checks, "
                "with the same sessions, parallelism and Run page as ever.")
    lines = ["Only scenarios made of service steps and assert_host_up run in the "
             "background."]
    for scenario, reason in blockers[:6]:
        lines.append("%s: %s" % (scenario, reason) if scenario else reason)
    if len(blockers) > 6:
        lines.append("… and %d more" % (len(blockers) - 6))
    return "\n".join(lines)


class MainWindow(QMainWindow):
    def __init__(self, splash=None, auto_launch=None, headless=False):
        super().__init__()
        self.splash = splash
        self._auto_launch = auto_launch
        self._headless = headless
        self._closing = False
        self.setWindowTitle("QAVector")
        self.setWindowIcon(icon.app_icon())
        self.resize(1380, 880)

        self.settings = Settings()
        self.core = core_mod.Core(self.settings.core_script, self.settings.interpreter,
                                  self.settings.config, flows_dir=self.settings.flows_path,
                                  cycles_dir=self.settings.cycles_path,
                                  secrets_path=self.settings.cycle_secrets_path,
                                  memory_path=self.settings.cycle_memory_path,
                                  runs_path=self.settings.cycle_runs_path)
        self._aim_log_sources()
        self.inventory = core_mod.Inventory()
        self.run_state = RunState(self)
        self.process = LauncherProcess(self)
        self.history = history_mod.History(parent=self)

        # The menu names things twice - plainly, and by the flag - and developer
        # mode picks. See widgets.Phrasing; the pages keep their own.
        self.wording = widgets.Phrasing()
        self._nav_buttons = {}
        self._nav_actions = {}       # nav key -> its View > Sidebar items entry
        self._nav_headings = []      # (heading widget, the keys under it)
        self._nav_labels = {key: label
                            for key, label, _mark in CONFIGURE + OBSERVE}
        # Which page is on screen, as opposed to which one was last remembered.
        # They differ while a hidden page is being moved off.
        self._page_key = self.settings.page
        self._run_source = self.settings.run_source
        self._entry_id = None
        self._stopping = False
        self._chrome_warned = False
        self._build_menu()
        self._build_ui()
        self._connect_process()
        # The desktop's title bar, replaced by one in the design. Nothing under
        # it is rebuilt - see titlebar.py, which also says how to keep the
        # desktop's own.
        self.frame = titlebar.install(self) if titlebar.wanted() else None

        self.set_developer_mode(self.settings.developer_mode)
        self.set_sidebar_collapsed(self.settings.sidebar_collapsed)
        if self.settings.always_on_top:
            self.set_always_on_top(True)
        self.show_page(self.settings.page)
        
        # Two labels rather than one string: the machine is read on a timer, the
        # worker count arrives on the event stream, and neither should have to
        # wait for the other's cadence to show what it knows.
        # What is up right now, wherever you are in the application. A service
        # started on one page and then left behind is the thing this exists for:
        # nothing else in the window would say it is still running.
        self.services_label = QLabel("")
        self.services_label.setStyleSheet("padding: 0 10px;")
        self.statusBar().addPermanentWidget(self.services_label)
        self.services.supervisor.status_changed.connect(self._update_services)
        self._update_services()

        # The same two facts on the rail, where they are visible from whichever
        # page you are on. The footer names what is up; the rail counts it, and
        # counts what has failed, which the footer has no room to say.
        self._badges_pending = False
        self.services.supervisor.status_changed.connect(self._update_nav_badges)
        # A service deleted while it was running is disposed of by sync without
        # a status of its own to report, so the save is the signal for that one.
        self.services.saved.connect(self._update_nav_badges)
        self.run_state.changed.connect(self._schedule_nav_badges)
        self._update_nav_badges()

        # A scenario can ask for a service - the other direction of the pipe the
        # launcher is already given. It needs the supervisor to act through and
        # the launcher's stdin to answer on, and this is where both exist.
        self.service_bridge = ServiceBridge(self.services.supervisor,
                                            self.process.send_command, self)
        # The same pipe again, carrying a question for the person at the screen
        # rather than one for a service. It needs nothing but somewhere to
        # answer on and a window to belong to.
        self.ask_bridge = AskBridge(self.process.send_command, self)

        self.workers_label = QLabel("")
        self.workers_label.setStyleSheet("padding: 0 10px;")
        self.statusBar().addPermanentWidget(self.workers_label)
        self.run_state.changed.connect(self._update_workers)
        self.metrics_label = QLabel("")
        self.metrics_label.setStyleSheet("padding: 0 10px;")
        self.statusBar().addPermanentWidget(self.metrics_label)

        self.loader_thread = None
        self._load = load_mod.Sampler()
        self._metrics_timer = QTimer(self)
        self._metrics_timer.timeout.connect(self._update_metrics)
        self._metrics_timer.start(2000)
        
        # Ask the core what exists as soon as the window is up, so the pages are
        # populated before the user reaches them.
        QTimer.singleShot(500 if self.splash else 80, self.refresh_inventory)
        if self.splash:
            QTimer.singleShot(SPLASH_MAX_MS, self._finish_splash)

    # -- construction ---------------------------------------------------------
    def _build_menu(self):
        menu = self.menuBar()

        file_menu = menu.addMenu("&File")
        reload_action = QAction("&Reload users.json", self)
        reload_action.setShortcut(QKeySequence("Ctrl+R"))
        reload_action.triggered.connect(lambda: self.credentials.load(self.core.config_path))
        file_menu.addAction(reload_action)
        save_action = QAction("&Save users.json", self)
        save_action.setShortcut(QKeySequence.Save)
        save_action.triggered.connect(lambda: self.credentials.save())
        file_menu.addAction(save_action)
        file_menu.addSeparator()
        settings_action = QAction("Se&ttings…", self)
        settings_action.triggered.connect(self.open_settings)
        file_menu.addAction(settings_action)
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        run_menu = menu.addMenu("&Run")
        self.run_action = QAction("&Run command", self)
        self.run_action.setShortcut(QKeySequence("Ctrl+Return"))
        self.run_action.triggered.connect(self.start_run)
        run_menu.addAction(self.run_action)
        self.stop_action = QAction("&Stop", self)
        self.stop_action.setShortcut(QKeySequence("Ctrl+."))
        self.stop_action.triggered.connect(self.stop_run)
        self.stop_action.setEnabled(False)
        run_menu.addAction(self.stop_action)

        view_menu = menu.addMenu("&View")
        self.developer_action = QAction("&Developer mode", self)
        self.developer_action.setCheckable(True)
        self.developer_action.setShortcut(QKeySequence("Ctrl+Shift+D"))
        self.developer_action.toggled.connect(self.set_developer_mode)
        view_menu.addAction(self.developer_action)
        
        self.always_on_top_action = QAction("Always on Top", self)
        self.always_on_top_action.setCheckable(True)
        self.always_on_top_action.toggled.connect(self.set_always_on_top)
        view_menu.addAction(self.always_on_top_action)
        
        self.dark_mode_action = QAction("&Dark mode", self)
        self.dark_mode_action.setCheckable(True)
        self.dark_mode_action.setChecked(self.settings.dark_mode)
        self.dark_mode_action.toggled.connect(self.set_dark_mode)
        view_menu.addAction(self.dark_mode_action)

        view_menu.addSeparator()
        self.collapse_action = QAction("&Collapse sidebar", self)
        self.collapse_action.setCheckable(True)
        self.collapse_action.setShortcut(QKeySequence("Ctrl+B"))
        self.collapse_action.toggled.connect(self.set_sidebar_collapsed)
        view_menu.addAction(self.collapse_action)

        # One entry per nav item, so a rail can be cut down to the pages this
        # particular person uses. Checked is shown, which is why the setting
        # stores what is hidden: a page added later is on the rail by default
        # rather than something to go and find.
        items_menu = view_menu.addMenu("Sidebar &items")
        hidden = set(self.settings.hidden_nav_items())
        for key, label, _mark in CONFIGURE + OBSERVE:
            # Doubled for the same reason as the rail's buttons: a menu entry
            # parses a single ampersand as its mnemonic.
            action = QAction(label.replace("&", "&&"), self)
            action.setCheckable(True)
            # Set before connecting: setChecked emits toggled, and a menu still
            # being built has no rail to apply it to.
            action.setChecked(key not in hidden)
            action.toggled.connect(
                lambda on, k=key: self.set_nav_item_visible(k, on))
            items_menu.addAction(action)
            self._nav_actions[key] = action

        tools_menu = menu.addMenu("&Tools")
        refresh_action = QAction(self)
        refresh_action.setShortcut(QKeySequence("F5"))
        refresh_action.triggered.connect(self.refresh_inventory)
        self.wording.text(refresh_action, "Refresh", "Refresh --describe")
        tools_menu.addAction(refresh_action)
        tools_menu.addSeparator()
        for plain, spelled_out, args in commands.ONE_SHOTS:
            action = QAction(self)
            # The window it opens is titled with whichever name the menu used, so
            # the two never disagree about what was asked for.
            action.triggered.connect(
                lambda _c=False, a=args, act=action: self.one_shot(act.text(), a))
            self.wording.text(action, plain, spelled_out)
            tools_menu.addAction(action)
        tools_menu.addSeparator()
        init_action = QAction("Create a starter users.json", self)
        init_action.triggered.connect(
            lambda: self.one_shot("Create a starter users.json",
                                  ["--init-users-json"]))
        tools_menu.addAction(init_action)

        help_menu = menu.addMenu("&Help")
        about_action = QAction("About", self)
        about_action.triggered.connect(self.about)
        help_menu.addAction(about_action)

    def _build_ui(self):
        central = QWidget()
        column = QVBoxLayout(central)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        # --- toolbar ---------------------------------------------------------
        bar = QFrame()
        bar.setProperty("role", "bar")
        bar_layout = QVBoxLayout(bar)
        bar_layout.setContentsMargins(12, 8, 12, 8)
        # A tool button rather than a plain one: RUN is the ordinary case and
        # stays one click, while recording is the same launch with the recorder
        # available - a mode of running, not a separate thing to go and find.
        self.run_button = QToolButton()
        self.run_button.setIcon(_run_icon())
        self.run_button.setText(_run_label("RUN"))
        self.run_button.setProperty("variant", "primary")
        self.run_button.setPopupMode(QToolButton.MenuButtonPopup)
        self.run_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.run_button.setProperty("hasmenu", "true")   # reserves the arrow's half
        self.run_button.clicked.connect(self.start_run)
        self.run_menu = QMenu(self.run_button)
        # The tooltips carry the reason an entry is greyed out, and a menu keeps
        # its tooltips to itself unless told otherwise.
        self.run_menu.setToolTipsVisible(True)
        # The same launch with no browser at all. Offered only for scenarios that
        # need none, which is decided as the menu opens, from what RUN would run
        # at that moment - see _update_background_action.
        self.background_action = QAction("In Background", self)
        self.background_action.triggered.connect(self.start_background_run)
        self.run_menu.addAction(self.background_action)
        # Whether recording continues a scenario or starts a new one follows from
        # what is selected, and is confirmed rather than guessed.
        self.record_action = QAction("With Recorder", self)
        self.record_action.setToolTip(
            "Open the windows with the Scenario Recorder shown in each of them. "
            "Continues the selected scenario if there is one.")
        self.record_action.triggered.connect(self.start_recording)
        self.run_menu.addAction(self.record_action)
        self.run_menu.aboutToShow.connect(self._update_background_action)
        self.run_button.setMenu(self.run_menu)
        # Stop mirrors RUN: the button is the whole run, the arrow is the one
        # window you are actually looking at. Same shape, because "stop" reads as
        # one idea with a narrower version of itself inside it.
        self.stop_button = QToolButton()
        self.stop_button.setText("Stop")
        icons.button(self.stop_button, "stop")
        self.stop_button.setPopupMode(QToolButton.MenuButtonPopup)
        self.stop_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.stop_button.setProperty("hasmenu", "true")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_run)
        self.stop_menu = QMenu(self.stop_button)
        # Filled from the live run: which windows exist is not knowable until one
        # is running, and stale entries would offer to stop what is already gone.
        self.stop_menu.aboutToShow.connect(self._fill_stop_menu)
        self.stop_button.setMenu(self.stop_menu)
        self.refresh_button = icons.button(QPushButton("Refresh"), "refresh")
        self.refresh_button.clicked.connect(self.refresh_inventory)
        self.describe_label = widgets.mono("")
        # Out of sight: which level the window is at is not something an everyday
        # user needs to read or flip, and the mode is still one keystroke away -
        # View -> Developer mode, Ctrl+Shift+D. The button is kept, and kept in
        # step by set_developer_mode, so it can come back by removing one line.
        self.developer_button = QPushButton("")
        self.developer_button.setCheckable(True)
        self.developer_button.setCursor(Qt.PointingHandCursor)
        self.developer_button.toggled.connect(self.set_developer_mode)
        self.developer_button.setVisible(False)
        settings_button = icons.button(QPushButton("Settings"), "settings")
        settings_button.clicked.connect(self.open_settings)
        bar_layout.addWidget(widgets.row(
            self.run_button, self.stop_button, widgets.vline(),
            self.refresh_button, None, self.describe_label, widgets.vline(),
            self.developer_button, settings_button))
        column.addWidget(bar)

        # --- body: rail + pages ---------------------------------------------
        body = QWidget()
        body_row = QVBoxLayout(body)
        body_row.setContentsMargins(0, 0, 0, 0)
        from PySide6.QtWidgets import QHBoxLayout
        inner = QHBoxLayout()
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(0)

        rail = QFrame()
        rail.setProperty("role", "sidebar")
        rail_layout = QVBoxLayout(rail)
        # 1px reserved on the right for the frame's own border. A stylesheet border
        # is painted inside the widget's rect and children are not clipped to the
        # contents rect, so a full-width child with any fill of its own paints over
        # it - which is how the divider ended up broken at the group headings and at
        # whichever nav item was active.
        rail_layout.setContentsMargins(0, 8, 1, 8)
        rail_layout.setSpacing(0)
        # The rail's own handle for the thing the View menu also switches. A
        # sidebar that can collapse should say so where it collapses; the menu
        # entry is for the person who wants the keystroke.
        handle_row = QHBoxLayout()
        handle_row.setContentsMargins(6, 0, 6, 2)
        handle_row.addStretch(1)
        self.collapse_button = QToolButton()
        self.collapse_button.setProperty("variant", "railhandle")
        self.collapse_button.setCursor(Qt.PointingHandCursor)
        self.collapse_button.clicked.connect(
            lambda: self.set_sidebar_collapsed(not self.settings.sidebar_collapsed))
        handle_row.addWidget(self.collapse_button)
        rail_layout.addLayout(handle_row)

        # The rail is sized from what is actually in it. A fixed width was fine
        # while the labels were short and the font was whatever this machine had,
        # but "Launch Sessions" and a letter-spaced "CONFIGURE" overflow it as soon
        # as the body font is wider - a condensed family missing, a larger system
        # font, display scaling - and a QFrame with a fixed width clips rather than
        # growing. So every label is measured and the widest one decides. The
        # collapsed width is measured the same way, from an icon-only button,
        # when the rail is first collapsed.
        widest = 0
        for title, entries in (("Configure", CONFIGURE), ("Observe", OBSERVE)):
            heading = widgets.kicker(title)
            heading.setContentsMargins(RAIL_PADDING, 8, RAIL_PADDING, 6)
            widest = max(widest, heading.sizeHint().width() - HEADING_TRIM)
            rail_layout.addWidget(heading)
            # Kept with the keys under it: a heading over a group whose every
            # item has been switched off names an empty space.
            self._nav_headings.append((heading, [k for k, _l, _m in entries]))
            for key, label, mark in entries:
                # A QToolButton, not a QPushButton: a push button centres its
                # icon and label as one block whatever text-align says, so the
                # rail's marks would slide left and right with the length of the
                # word beside them. Text-beside-icon on a tool button is the one
                # arrangement Qt keeps flush left.
                # Doubled ampersand: a button parses a single one as a keyboard
                # mnemonic and draws the next letter underlined instead, which is
                # how "Services & Logs" came out of the rail as "Services _Logs".
                # Only what is drawn is escaped - _nav_labels keeps the real name,
                # so the collapsed rail's tooltip still reads properly.
                # Only the entries that have something to count ask for the room
                # to say it: reserving it on every one of them would widen the
                # rail on account of "Launch Sessions", which never counts.
                button = icons.button(widgets.NavButton(NAV_BADGES.get(key, 0)),
                                      mark, label.replace("&", "&&"))
                button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
                button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                button.setProperty("variant", "nav")
                button.setCursor(Qt.PointingHandCursor)
                button.clicked.connect(lambda _c=False, k=key: self.show_page(k))
                self._nav_buttons[key] = button
                # Measured from the label alone, counts or no counts: the rail
                # is the width its names need and a tally is not a name. What is
                # left past the longest of them is what the counts get, and an
                # entry with less than they need says it in colour instead of
                # asking for more - see widgets.NavButton.
                widest = max(widest, button.sizeHint().width())
                rail_layout.addWidget(button)
            rail_layout.addSpacing(10)
        rail_layout.addStretch(1)
        self._rail = rail
        self._rail_width = max(RAIL_MIN_WIDTH, widest + RAIL_SLACK)
        rail.setFixedWidth(self._rail_width)
        self.rail_note = QLabel("")
        self.rail_note.setStyleSheet(
            "font-family: %s; font-size: 10px; color: %s; border-top: 1px solid %s;"
            "padding: 8px 14px;" % (theme.MONO_CSS, theme.NEUTRAL[600], theme.DIVIDER))
        self.rail_note.setWordWrap(True)
        rail_layout.addWidget(self.rail_note)
        inner.addWidget(rail)

        self.stack = QStackedWidget()
        self.environments = EnvironmentsPage(self.settings)
        self.credentials = CredentialsPage()
        self.services = ServicesPage(self.settings)
        self.services.set_core(self.core)
        self.scenarios = ScenariosPage(self.settings)
        self.cycles = CyclesPage(self.settings)
        self.cycles.set_run_state(self.run_state)
        self.cycles.cycle_opened.connect(self._restore_selected_cycle)
        self.cycles.session_opened.connect(self._open_cycle_run)
        self.command = CommandPage(self.settings)
        self.launch = LaunchSessionsPage(self.settings)
        self.run = RunPage(self.run_state)
        self.log = LogPage(self.settings)
        self.artifacts = ArtifactsPage(self.settings)
        self.history_page = HistoryPage(self.history)
        # Both observing pages read the run record for what to offer, so they open
        # on the last run rather than on nothing after a restart.
        self.log.set_history(self.history)
        self.artifacts.set_history(self.history)
        self._pages = {"environments": self.environments, "credentials": self.credentials,
                       "logsources": self.services,
                       "scenarios": self.scenarios,
                       "cycles": self.cycles,
                       "commands": self.command, "launch": self.launch,
                       "run": self.run, "log": self.log,
                       "artifacts": self.artifacts, "history": self.history_page}
        for page in self._pages.values():
            self.stack.addWidget(page)
        inner.addWidget(self.stack, 1)
        body_row.addLayout(inner)
        column.addWidget(body, 1)

        self.command.run_requested.connect(lambda: self.start_run("commands"))
        self.launch.run_requested.connect(lambda: self.start_run("launch"))
        self.credentials.saved.connect(self.refresh_inventory)
        # A new log source changes what --server-log can pick, and the
        # Launch page builds its picker from the inventory.
        self.services.saved.connect(self.refresh_inventory)
        # Writing a scenario changes what --run-tests can run, so the inventory
        # every other page reads has to be re-read.
        self.scenarios.saved.connect(self.refresh_inventory)
        # A cycle names the scenarios and services it runs, so
        # writing one changes nothing else - but the inventory is
        # what its own list is built from.
        self.cycles.saved.connect(self.refresh_inventory)
        self.cycles.run_requested.connect(self.start_cycle_run)
        self.cycles.stop_requested.connect(self.stop_run)
        self.environments.directories_changed.connect(self._directories_changed)
        self.run_state.run_dir_known.connect(self.artifacts.set_run_dir)
        # Recorded the moment the run says where it is writing, not only when
        # it ends: a run the application was killed during is exactly the one
        # somebody wants back, and it never reaches _close_entry.
        self.run_state.run_dir_known.connect(self._note_run_dir)
        self.run_state.artifacts_written.connect(self.artifacts.note_artifacts)
        self.history_page.rerun_requested.connect(self.rerun_entry)
        self.history_page.restore_requested.connect(self.restore_entry)
        self.history_page.open_log_requested.connect(self._open_log)
        self.history_page.open_artifacts_requested.connect(self._open_artifacts)

        self.setCentralWidget(central)

        # --- status bar ------------------------------------------------------
        # Which core and which interpreter are settings, not state: they are
        # decided once in Settings, they are on About, and the status bar is for
        # what is happening now. The config file stays - it is the one of the
        # three that changes what a run reads.
        self.status_config = QLabel("")
        self.status_state = QLabel("idle")
        for label in (self.status_config, self.status_state):
            label.setStyleSheet("font-family: %s; font-size: 11px; color: %s;"
                                % (theme.MONO_CSS, theme.NEUTRAL[700]))
            self.statusBar().addWidget(label)
        self.status_right = QLabel("ready")
        self.status_right.setStyleSheet("font-family: %s; font-size: 11px; color: %s;"
                                        % (theme.MONO_CSS, theme.NEUTRAL[600]))
        self.statusBar().addPermanentWidget(self.status_right)
        self._update_status()

    def _connect_process(self):
        self.process.event.connect(self._on_event)
        self.process.log.connect(self.log.append)
        self.process.started.connect(self._on_started)
        self.process.finished.connect(self._on_finished)
        self.process.failed.connect(self._on_failed)

    # -- navigation -----------------------------------------------------------
    def show_page(self, key):
        if key in DEVELOPER_ONLY and not self.settings.developer_mode:
            key = "launch"
        # A page switched off in View is not reachable, including the one that
        # was remembered from last time and the "launch" fallback above.
        if not self._offered(key) and key in self._pages:
            key = self._first_offered() or key
        page = self._pages.get(key)
        if page is None:
            key, page = "launch", self.launch
        self.stack.setCurrentWidget(page)
        for name, button in self._nav_buttons.items():
            button.setProperty("active", "true" if name == key else "false")
            button.style().unpolish(button)
            button.style().polish(button)
        self._page_key = key
        self.settings.page = key
        # RUN follows whichever of the two launching pages you were last in, so
        # the toolbar button and Ctrl+Return never act on a page you left behind.
        if key in ("launch", "commands"):
            self._run_source = key
            self.settings.run_source = key
            self._update_run_label()

    # -- the rail: what it holds, and how much of it it shows -----------------
    def _offered(self, key, hidden=None):
        """Whether ``key`` is on the rail: not switched off, and in this mode."""
        if hidden is None:
            hidden = set(self.settings.hidden_nav_items())
        if key in hidden:
            return False
        return key not in DEVELOPER_ONLY or self.settings.developer_mode

    def _first_offered(self):
        for key, _label, _mark in CONFIGURE + OBSERVE:
            if self._offered(key):
                return key
        return None

    def set_nav_item_visible(self, key, visible):
        """Put a page on the rail, or take it off. The View menu's per-item switch."""
        visible = bool(visible)
        hidden = set(self.settings.hidden_nav_items())
        if visible:
            hidden.discard(key)
        else:
            hidden.add(key)
        if not any(self._offered(k, hidden) for k, _l, _m in CONFIGURE + OBSERVE):
            # A rail with nothing in it is a window with no way out of the page
            # it happens to be on. Refuse the last one and say so by putting the
            # tick back.
            action = self._nav_actions.get(key)
            if action is not None:
                action.blockSignals(True)
                action.setChecked(True)
                action.blockSignals(False)
            return
        self.settings.save_hidden_nav_items(hidden)
        self._apply_nav_visibility()

    def _apply_nav_visibility(self):
        """Bring the rail, the menu ticks and the current page into agreement.

        The single place that decides what shows: both switches feed into it -
        developer mode, which owns the Command page, and the per-item ones - so
        neither can undo the other by writing straight to a button.
        """
        hidden = set(self.settings.hidden_nav_items())
        if not any(self._offered(k, hidden) for k, _l, _m in CONFIGURE + OBSERVE):
            # Reachable by leaving developer mode with nothing but developer
            # pages left switched on. Give Launch Sessions back rather than
            # stranding the window.
            hidden.discard("launch")
            self.settings.save_hidden_nav_items(hidden)
        collapsed = self.settings.sidebar_collapsed
        for key, button in self._nav_buttons.items():
            button.setVisible(self._offered(key, hidden))
        for heading, keys in self._nav_headings:
            heading.setVisible(not collapsed
                               and any(self._offered(k, hidden) for k in keys))
        for key, action in self._nav_actions.items():
            action.blockSignals(True)
            action.setChecked(key not in hidden)
            action.blockSignals(False)
        # If the page on screen is the one that just went away, move off it.
        self.show_page(self._page_key)

    def set_sidebar_collapsed(self, collapsed):
        """Show the rail as marks only, or as marks and their labels."""
        collapsed = bool(collapsed)
        self.settings.sidebar_collapsed = collapsed
        self.collapse_action.blockSignals(True)
        self.collapse_action.setChecked(collapsed)
        self.collapse_action.blockSignals(False)

        for key, button in self._nav_buttons.items():
            button.setToolButtonStyle(Qt.ToolButtonIconOnly if collapsed
                                      else Qt.ToolButtonTextBesideIcon)
            # Marks only, so the counts go on the mark's own corner: the
            # collapsed rail is a width, and neither a figure nor the room for
            # one fits beside a mark - see NavButton.
            button.set_compact(collapsed)
            button.setToolTip(self._nav_tooltip(key))
        self.rail_note.setVisible(not collapsed)
        icons.button(self.collapse_button, "expand" if collapsed else "collapse")
        self.collapse_button.setToolTip("Expand the sidebar" if collapsed
                                        else "Collapse the sidebar")
        self._apply_nav_visibility()          # the headings follow the state

        if collapsed:
            # Measured, like the expanded width and for the same reason: what an
            # icon-only button comes to is the style's doing and the screen's,
            # not anything set here.
            marks = max(button.sizeHint().width()
                        for button in self._nav_buttons.values())
            self._rail.setFixedWidth(max(RAIL_COLLAPSED_MIN_WIDTH, marks + 1))
        else:
            self._rail.setFixedWidth(self._rail_width)

    def set_always_on_top(self, enabled):
        enabled = bool(enabled)
        self.settings.always_on_top = enabled
        self.always_on_top_action.blockSignals(True)
        self.always_on_top_action.setChecked(enabled)
        self.always_on_top_action.blockSignals(False)
        was_visible = self.isVisible()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, enabled)
        # Changing a flag on a mapped window makes Qt destroy and re-create it,
        # so it has to be shown again or it simply disappears - but ONLY if it
        # was on screen to begin with. At startup this runs while the splash is
        # still up and the pages are empty; showing here put a half-built window
        # on screen and left the data arriving a second or two later.
        if was_visible:
            self.show()
            if enabled:
                self.raise_()
                self.activateWindow()

    def _finish_splash(self):
        """Take the splash down and show the window. Safe to call twice.

        Reached three ways: the load finishing, the load failing, and the hard
        timeout armed at startup - whichever comes first wins, and the rest are
        no-ops.
        """
        if not self.splash:
            return
        if not self._headless:
            self.show()
        self.splash.finish(self)
        self.splash = None

    def _keep_on_top(self):
        """Restack above a window that has just mapped, if asked to stay on top.

        A run opens seven browsers, each mapping and raising itself. The window
        manager honours the always-on-top state, but re-asserting costs nothing
        and covers the window that maps in the same instant the flag is applied.

        raise_() only, never activateWindow(): the auto-login extension is typing
        into one of those browsers, and taking keyboard focus off it mid-login is
        how a launch loses its password.
        """
        if self.always_on_top_action.isChecked():
            self.raise_()

    def _update_workers(self):
        """Show the number of sessions in force right now, and what last set it.

        Shown for every parallel run, not only the governed ones: "how many are
        running" is a fair question whether or not the answer can change, and it
        is the launch page's number confirmed rather than repeated - that one is
        what was asked for, this one is what the run is doing.
        """
        workers = self.run_state.workers
        if not workers or not self.process.is_running():
            self.workers_label.clear()
            self.workers_label.setToolTip("")
            return
        limit, ceiling = workers["limit"], workers["ceiling"]
        held_back = limit is not None and ceiling is not None and limit < ceiling
        self.workers_label.setText(
            "<span style='color: %s'>%s of %s</span>"
            % (theme.WARN if held_back else theme.NEUTRAL[600], limit, ceiling))
        self.workers_label.setToolTip(
            "%s %s running at once, out of %s.\nLast change: %s"
            % (limit, workers["unit"], ceiling, workers["why"] or "none yet"))

    def _update_metrics(self):
        load = self._load.read()
        used = load.used_percent
        if not load.readable:
            self.metrics_label.clear()
            self._metrics_timer.stop()   # this platform will not start answering
            return
        # Red for stall, not for a busy CPU: a saturated machine driving windows
        # is the tool working. Only memory pressure threatens the run.
        strained = (load.mem_stall is not None and load.mem_stall > 10) or \
                   (used is not None and used > 90)
        colour = theme.BAD if strained else theme.NEUTRAL[600]
        self.metrics_label.setText(
            "<span style='color: %s'>CPU %s &nbsp;RAM %s</span>"
            % (colour,
               "--" if load.cpu_percent is None else "%.0f%%" % load.cpu_percent,
               "--" if used is None else "%.0f%%" % used))

    def set_dark_mode(self, enabled):
        from PySide6.QtWidgets import QApplication
        enabled = bool(enabled)
        self.settings.dark_mode = enabled
        
        self.dark_mode_action.blockSignals(True)
        self.dark_mode_action.setChecked(enabled)
        self.dark_mode_action.blockSignals(False)
        
        theme.set_dark_mode(enabled)
        QApplication.instance().setStyleSheet(theme.stylesheet())

    def set_developer_mode(self, enabled):
        enabled = bool(enabled)
        self.settings.developer_mode = enabled
        # Both are checkable and both call back in here, so each has to be set
        # quietly or the two would bounce the change between them.
        for control in (self.developer_button, self.developer_action):
            control.blockSignals(True)
            control.setChecked(enabled)
            control.blockSignals(False)
        icons.button(self.developer_button, "developer",
                     "Developer mode: on" if enabled else "Developer mode: off")
        self.developer_button.setProperty("variant", "primary" if enabled else "")
        self.developer_button.style().unpolish(self.developer_button)
        self.developer_button.style().polish(self.developer_button)

        self._apply_nav_visibility()
        # The wording is part of the mode: a flag name is noise on one level and
        # the whole answer on the other. Every page that says anything in both
        # registers gets told, rather than this window knowing which they are.
        self.wording.apply(enabled)
        for page in self._pages.values():
            follow = getattr(page, "set_developer_mode", None)
            if follow is not None:
                follow(enabled)
        if not enabled and self.settings.page in DEVELOPER_ONLY:
            self.show_page("launch")
        else:
            self._update_run_label()

    def _update_run_label(self):
        """Say which page RUN will act on, but only when both are reachable."""
        if self.settings.developer_mode and self._run_source == "commands":
            self.run_button.setText(_run_label("RUN COMMAND"))
        else:
            self.run_button.setText(_run_label("RUN"))

    def _run_page(self, source=None):
        source = source or self._run_source
        if source == "commands" and self.settings.developer_mode:
            return "commands", self.command
        return "launch", self.launch

    def _directories_changed(self):
        self.command.set_inventory(self.inventory)
        self.launch.set_inventory(self.inventory)

    # -- core -----------------------------------------------------------------
    def refresh_inventory(self):
        # The first refresh is a delayed singleShot, so a window closed inside
        # that delay would otherwise start a describe on its way out - a thread
        # nobody is left to wait for, and widgets nobody is left to fill in.
        if self._closing:
            return
        if self.loader_thread and self.loader_thread.isRunning():
            return

        self.command.set_core(self.core)
        self.launch.set_core(self.core)
        # Scenarios reads and writes files through the core too, not just runs it.
        self.scenarios.set_core(self.core)
        self.cycles.set_core(self.core)
        
        def _splash_msg(text):
            """Progress, for the splash only.

            Deliberately not the top bar's describe label: stage names like
            "Parsing inventory..." are this window talking to itself, and one
            left showing after a slow load reads as a state the app is stuck in.
            That label says one quiet thing while loading, then the summary.
            """
            if self.splash:
                # One line; the splash draws it into its own status strip, so
                # neither the padding nor the colour belongs here any more.
                self.splash.showMessage("%s  \u00B7  %s" % (gui_version(), text))
                self.splash.repaint()
                QApplication.processEvents()

        def _on_error(exc):
            self.describe_label.setText("could not read the configuration")
            self.status_right.setText("check Settings -> Core script")
            self._finish_splash()
            if self._headless:
                logging.error("Cannot read the core: %s", exc)
                QApplication.quit()
                return
            widgets.warn(self, "Cannot read the core",
                                "%s\n\nCheck Settings -> Core script / Interpreter."
                                % exc)
                                
        def _on_finished(inventory):
            self.inventory = inventory
            
            _splash_msg("Populating environments...")
            self.environments.set_inventory(self.inventory)
            
            _splash_msg("Populating commands...")
            self.command.set_inventory(self.inventory)
            
            _splash_msg("Populating launchers...")
            self.launch.set_inventory(self.inventory)
            
            _splash_msg("Populating scenarios...")
            self.scenarios.set_inventory(self.inventory)
            
            _splash_msg("Populating cycles...")
            # The projects file is the Cycles page's own - the launcher has
            # never heard of it - so the path comes from Settings here rather
            # than travelling with every call the way --flows-dir does.
            self.cycles.set_projects_path(self.settings.cycle_projects_path)
            self.cycles.set_inventory(self.inventory)
            
            _splash_msg("Loading credentials...")
            self.credentials.load(self.inventory.config_path or self.core.config_path)

            _splash_msg("Loading services and logs...")
            self.services.set_environments([env.get("value", "")
                                            for env in self.inventory.envs])
            self.services.load(self.core.legacy_log_sources
                               or self.inventory.log_sources_path,
                               self.settings.services_path,
                               self.settings.log_sources_path)
            # Loading replaces the supervisor's whole set, and a service that
            # simply stopped existing reports no status on its way out - so the
            # window's two tallies have to be asked again rather than waiting to
            # be told, or they go on naming what is no longer configured.
            self._update_services()
            self._update_nav_badges()

            _splash_msg("Finalizing UI...")
            self.describe_label.setText(self.inventory.summary())
            self.rail_note.setText("core %s\nPySide6 %s" % (self.inventory.version,
                                                            _pyside_version()))
            if self.inventory.warnings:
                self.status_right.setText(self.inventory.warnings[0][:90])
            self._update_status()
            self._warn_about_chrome()
            
            if self.splash:
                _splash_msg("Ready!")
                QTimer.singleShot(SPLASH_READY_MS, self._finish_splash)
            else:
                self._finish_splash()
                
            # The last cycle run, put back on its page. After the inventory,
            # because it opens a cycle; through the event loop, because a file
            # read and a --cycle-show are no reason to hold up the splash.
            QTimer.singleShot(0, self._restore_last_cycle)

            if self._auto_launch:
                self.show_page("launch")
                self.launch._reload_configs(select=self._auto_launch)
                QTimer.singleShot(100, self.launch.run_requested.emit)
                self._auto_launch = None
                
        self.describe_label.setText("reading the configuration …")
        self.loader_thread = LoaderThread(self.core, self)
        self.loader_thread.progress.connect(_splash_msg)
        self.loader_thread.error.connect(_on_error)
        self.loader_thread.finished_inventory.connect(_on_finished)
        self.loader_thread.start()

    def _restore_selected_cycle(self, cycle_id):
        if not self.process.is_running():
            cyclereplay.restore_cycle(self.history, self.run_state, cycle_id)

    def _open_cycle_run(self, cycle_id, run_dir):
        """Put one earlier run on the Cycles page - a session picked on the right.

        Replayed first and opened after, the way _restore_last_cycle does it:
        opening a cycle paints whatever the model holds, so the model has to
        hold the run before the canvas asks.
        """
        if self.process.is_running() or not run_dir:
            return
        if cyclereplay.restore(run_dir, self.run_state):
            self.cycles.open(cycle_id)

    def _restore_last_cycle(self):
        """Bring the last cycle run back onto the Cycles page.

        Everything a cycle reported used to live only in memory, so closing the
        window lost the graph's colours, the output and the stages of a run
        that may have taken an hour. The run's own directory has all of it -
        see cyclereplay - and reading it back is the difference between coming
        back to yesterday's run and coming back to a blank page.

        A run started in the meantime wins: this is a courtesy to somebody who
        has just opened the window, not something that may overwrite what they
        are actually watching.
        """
        if self.process.is_running():
            return
        cycle_id = cyclereplay.restore_last(self.history, self.run_state)
        if cycle_id:
            self.cycles.open(cycle_id)

    def _warn_about_chrome(self):
        """Say so, once, when the core cannot find a browser to launch.

        Everything this app does ends in a Chrome window, so learning at startup
        beats learning halfway through a launch. Shown once per session: it is a
        prerequisite the user has to go away and fix, not a recurring alert.
        """
        problem = self.inventory.chrome_problem()
        if not problem:
            self._chrome_warned = False
            return
        self.status_right.setText("Google Chrome not found")
        if getattr(self, "_chrome_warned", False):
            return
        self._chrome_warned = True
        widgets.warn(self, "Google Chrome is required", problem)

    def open_settings(self):
        dialog = SettingsDialog(self.settings, self)
        dialog.set_paths(*self.services.paths())
        dialog.set_flows_dir(self.inventory.dirs().get("flows") or "")
        if dialog.exec() == QDialog.Accepted:
            dialog.apply()
            self.core = core_mod.Core(self.settings.core_script,
                                      self.settings.interpreter, self.settings.config,
                                      flows_dir=self.settings.flows_path,
                                      cycles_dir=self.settings.cycles_path,
                                      secrets_path=self.settings.cycle_secrets_path,
                                      memory_path=self.settings.cycle_memory_path,
                                      runs_path=self.settings.cycle_runs_path)
            self._aim_log_sources()
            self.refresh_inventory()

    def one_shot(self, title, args):
        """Run a command that just prints something, and show what it printed."""
        if not self.core.is_configured():
            widgets.note(self, title, "Configure the core first "
                                                 "(Settings -> Core script).")
            return
        try:
            code, out, err = self.core.run(*args)
        except core_mod.CoreError as exc:
            widgets.warn(self, title, str(exc))
            return
        widgets.output(self, "%s (exit %d)" % (title, code), title,
                       (out or "") + ("\n" + err if err else ""),
                       kind="" if code == 0 else "error")
        if args == ["--init-users-json"]:
            self.refresh_inventory()

    # -- running --------------------------------------------------------------
    def recordable_scenario(self):
        """The scenario a recording would continue, or "".

        Whatever is open in the Scenarios editor comes first - that is the one
        being worked on. Otherwise a single scenario chosen on Launch Sessions,
        since picking exactly one and then recording plainly means that one. Only
        ever something writable: a bundled scenario cannot be recorded into.
        """
        current = self.scenarios.current
        if current and current.get("writable"):
            return current["id"]
        chosen = [s for s in self.launch.state().get("scenarios", {}).get("selected", [])
                  if not str(s).startswith("tag:")]
        if len(chosen) == 1:
            row = self.inventory.scenario(chosen[0])
            if row.get("writable"):
                return chosen[0]
        return ""

    def ask_recording_target(self):
        """Decide what a recording writes to: ("continue", id) / ("new", "") / cancel.

        With nothing selected there is nothing to ask about. With something
        selected, asking is the point: appending to a scenario and replacing one
        look identical until it is too late, and the answer is a click either way.

        Split from the launch so the decision can be tested without a dialog.
        """
        scenario = self.recordable_scenario()
        if not scenario:
            return "new", ""
        keep = "Continue \"%s\"" % scenario
        answer = widgets.choose(
            self, "Record", "\"%s\" is selected." % scenario,
            "Continue recording into it - its steps are loaded and what you "
            "capture is added to them - or start a new scenario?",
            choices=("Start new",), agree=keep)
        if answer == keep:
            return "continue", scenario
        if answer == "Start new":
            return "new", ""
        return "cancel", ""

    def start_recording(self):
        """Launch with the recorder available, from whichever page RUN would use.

        Deliberately the same launch: same environment, same accounts, same
        profiles. The only difference is that the windows carry a debug port and
        show the recorder, and that nothing is run afterwards - the launcher
        stays up for as long as you are recording.
        """
        login = self.ask_recording_account()
        if login == "cancel":
            return
        choice, scenario = self.ask_recording_target()
        if choice == "cancel":
            return
        self.start_run(recorder=scenario or True, only_login=login or None)

    def ask_recording_account(self):
        """Which single account to record: a login, "" (leave as configured), or "cancel".

        Recording is a one-window activity - you can only be clicking in one of
        them - and the core refuses more than one outright. Asking here turns
        that refusal into a choice, at the moment the choice is obvious.
        """
        source, page = self._run_page(None)
        if source != "launch" or getattr(page, "inventory", None) is None:
            return ""       # the Command page states its own accounts; let the core judge
        logins = launch_mod.selected_logins(page.state(), page.inventory)
        if len(logins) <= 1:
            return ""
        # A list, not a row of buttons: eleven accounts is a normal number here.
        chosen, ok = QInputDialog.getItem(
            self, "Record",
            "Recording opens one window, and %d accounts are selected.\n"
            "Which one do you want to record?" % len(logins),
            logins, 0, False)
        if not ok or not chosen:
            return "cancel"
        return chosen

    def _update_background_action(self):
        """Offer In Background only for what can run with no browser, and say why not.

        Asked as the menu opens rather than kept in step with every edit: it is
        only read then, and what RUN would run depends on which page is current at
        that moment. Returns the blockers, empty when there are none.
        """
        _source, page = self._run_page(None)
        blockers = page.background_blockers()
        self.background_action.setEnabled(not blockers)
        self.background_action.setToolTip(_background_tip(blockers))
        return blockers

    def start_background_run(self):
        """RUN's menu -> In Background: the same launch, with no browser at all.

        Sessions, how many run at once and the load governor are exactly as the
        page says; only the windows are missing. Checked again here rather than
        trusted from when the menu opened, so a stale menu cannot start a run the
        launcher would refuse.
        """
        if self._update_background_action():
            return
        self.start_run(background=True)

    def start_run(self, source=None, recorder=False, only_login=None, background=False):
        if self.process.is_running():
            return
        if not self.core.is_configured():
            widgets.note(self, "Run", "Configure the core first "
                                                 "(Settings -> Core script).")
            return
        source, page = self._run_page(source)
        if source == "launch":
            problems = page.problem_list()
            if problems:
                if self._headless:
                    logging.error("Cannot launch, problems: %s", problems)
                    QApplication.quit()
                    return
                widgets.note(self, "Launch Sessions",
                                        "Fix this first:\n\n- %s"
                                        % "\n- ".join(problems))
                return
        args = page.argv()
        if background:
            args = commands.for_background(args)
        if recorder:
            args = commands.for_recording(args)
            if only_login:
                # Replace rather than add: the configuration's own account list is
                # what made this ambiguous, and the core takes one --filter-users.
                args = [a for a in args if not a.startswith("--filter-users=")]
                args.insert(0, "--filter-users=%s" % only_login)
            # Ahead of --events, which build_argv always puts last.
            args.insert(0, "--recorder" if recorder is True
                        else "--recorder=%s" % recorder)
        try:
            argv = self.core.argv(*args)
        except core_mod.CoreError as exc:
            if self._headless:
                logging.error("CoreError: %s", exc)
                QApplication.quit()
                return
            widgets.warn(self, "Run", str(exc))
            return
        self.run_state.reset()
        self.log.clear()
        self._stopping = False
        self._entry_id = self.history.begin(
            *self._history_payload(source, page, args, argv, background))
        if recorder:
            self.run.run_started(
                ("continuing %s · " % recorder if recorder is not True else "")
                + "recorder · press Capture Step (or F2) in a window")
        elif source == "launch":
            self.run.run_started("%s · events on stdout" % page.run_meta(background))
        else:
            state = page.state()
            self.run.run_started("%sjobs=%s · %s · events on stdout"
                                 % ("in the background · " if background else "",
                                    state.get("--jobs") or "1",
                                    state.get("--run-tests") or "(launch only)"))
        self.show_page("run")
        self.process.start(argv, working_dir=self.core.spawn_dir())

    def _history_payload(self, source, page, args, argv, background=False):
        """(kind, entry fields) for the run about to start.

        Both halves go in: what the user asked for, and the command it became.
        The first is what "run it again, but with one change" needs; the second is
        what makes the entry readable without the GUI at all.
        """
        try:
            display = self.core.display_argv(*args)
        except core_mod.CoreError:
            display = " ".join(argv)
        if source == "launch":
            config = page.state()
            return history_mod.LAUNCH, {
                "argv": list(argv), "display_command": display,
                "summary": page.describe_line(background), "launch_config": config,
                "command_state": launch_mod.to_command_state(config, self.inventory)}
        state = page.state()
        return history_mod.COMMAND, {
            "argv": list(argv), "display_command": display,
            "summary": commands.preview(state, self.core), "command_state": state}

    def _fill_stop_menu(self):
        """One entry per window still running, plus everything.

        Rebuilt each time it opens rather than kept in step with the run: a menu
        that is only correct while it is on screen only has to be correct then.
        """
        self.stop_menu.clear()
        live = self.run_state.live()
        for session in live:
            label = session["name"]
            if session.get("scenario"):
                label += "  (%s)" % session["scenario"]
            action = QAction(label, self)
            action.setToolTip("Stop this window and close it. The others carry on.")
            action.triggered.connect(
                lambda _checked=False, name=session["name"]: self.stop_session(name))
            self.stop_menu.addAction(action)
        if not live:
            nothing = QAction("No windows running", self)
            nothing.setEnabled(False)
            self.stop_menu.addAction(nothing)
            return
        self.stop_menu.addSeparator()
        everything = QAction("Stop everything", self)
        everything.triggered.connect(self.stop_run)
        self.stop_menu.addAction(everything)

    def stop_session(self, name):
        """Stop ONE window: it closes, the rest of the run carries on."""
        if not self.process.is_running():
            return
        if not self.process.send_command(command="stop-session", session=name):
            return
        self.run_state.mark_stopping(name)
        self.status_right.setText("stopping %s — the others carry on" % name)

    def start_cycle_run(self, cycle_id, how="", step=""):
        """Run one cycle. Modelled on start_run, and deliberately separate.

        A scenario run is assembled from a page's form; a cycle run is one id
        and the flags the core needs to report it. Folding the two together
        would mean a page-shaped branch inside start_run for something that
        shares only its guards.

        It stays on the Cycles page rather than switching to Run: the canvas
        already on screen *is* the run view, and sending somebody to a second
        picture of it would be the one thing this feature is meant not to do.

        ``how`` and ``step`` run part of it: "only" for that step by itself,
        "from" for it and everything that waits on it. Whichever steps are left
        out the core takes from the displayed run (or the latest if none is
        displayed), so what they
        produced is still in scope. How that is spelled on a command line is
        settled here, because this is where every other flag is spelled too.
        """
        if not cycle_id or self.process.is_running():
            return
        if not self.core.is_configured():
            widgets.note(self, "Run", "Configure the core first "
                                                 "(Settings -> Core script).")
            return
        args = (["--cycle-run=" + cycle_id, "--events=-", "--control=-"]
                + self.core.secrets_flag() + self.core.memory_flag())
        if how == "resume" and step:
            args.append("--cycle-resume=" + step)
        elif how and step:
            args.append("--cycle-%s=%s" % ("only" if how == "only" else "from",
                                           step))
            displayed = self.run_state.cycle or {}
            if displayed.get("cycle") == cycle_id and displayed.get("run_id"):
                args.append("--cycle-reuse=" + displayed["run_id"])
        try:
            argv = self.core.argv(*args)
        except core_mod.CoreError as exc:
            widgets.warn(self, "Run", str(exc))
            return

        self.run_state.reset()
        # The cycle record has its own lifetime - a scenario run no longer
        # clears it - so the one place that starts a cycle says so.
        if how != "resume":
            self.run_state.reset_cycle()
        self.log.clear()
        self._stopping = False
        self._entry_id = self.history.begin(history_mod.CYCLE, {
            "summary": cycle_id,
            "argv": argv,
            "display_command": self.core.display_argv(*args),
            "config": {"cycle": cycle_id},
        })
        self.run.run_started("cycle %s · events on stdout" % cycle_id)
        self.show_page("cycles")
        self.process.start(argv, working_dir=self.core.spawn_dir())

    def stop_run(self):
        if not self.process.is_running():
            return

        window_count = len(self.run_state.sessions)
        msg = "Are you sure you want to stop the current run?"
        if window_count > 0:
            msg += f"\nThis will close {window_count} window{'s' if window_count != 1 else ''}."
            
        if not widgets.confirm(self, "Confirm Stop", msg, agree="Stop",
                               kind="warn"):
            return
            
        self._stopping = True
        self.status_right.setText("stopping — the launcher closes its windows first")
        self.process.stop()

    def _on_started(self, argv):
        self._set_running(True)
        self.log.append({"ts": "", "level": "INFO", "session": "",
                         "text": "$ " + " ".join(argv)})

    def _on_event(self, event):
        self.run_state.handle(event)
        kind = event.get("kind")
        if kind == "service.request":
            # A scenario is blocked on the answer to this, so it goes first and
            # nothing else here is interested in it.
            self.service_bridge.handle(event)
            return
        if kind in ("cycle.ask", "cycle.ask.withdrawn"):
            # A step is blocked on a person answering this. Handled here rather
            # than on the cycle page for the same reason a service request is:
            # nothing else is interested, and the answer goes back on the
            # launcher's stdin, which lives here.
            self.ask_bridge.handle(event)
            return
        if kind == "window.launched":
            self._keep_on_top()
        elif kind == "run.finished":
            # Settle the Run page now. The launcher may well still be up - with
            # the windows kept open it is - so Stop stays enabled and the run is
            # not marked over; only the page stops pretending steps are running.
            self.run.run_finished(event.get("exit_code") or 0)
            if self.process.is_running():
                self.status_right.setText(
                    "scenarios finished — windows open, Stop closes them")
        # A recording is scenario work, not run work, so it is shown where
        # scenarios live rather than in the run view.
        if str(event.get("kind", "")).startswith("recorder."):
            self.scenarios.handle_recorder_event(event)
        # A cycle draws itself on its own page, on the same canvas it was read
        # into - there is no second view of a run to keep in step with it.
        elif str(event.get("kind", "")).startswith("cycle."):
            self.cycles.handle_event(event)

    def _on_finished(self, code):
        self._set_running(False)
        # Nothing left to answer: the timers would otherwise outlive the run and
        # write into a launcher that has gone.
        self.service_bridge.cancel_all()
        self.ask_bridge.cancel_all()
        # A core that went without sending cycle.run.end - killed, crashed -
        # would otherwise leave the Cycles page offering Stop for a run that
        # is not there, and steps spinning forever.
        self.run_state.cycle_interrupted()
        self.run.run_finished(code)
        self.status_right.setText("finished (exit %d)" % code)
        if self.run_state.run_dir:
            self.artifacts.set_run_dir(self.run_state.run_dir)
        self._close_entry(history_mod.status_for(code, stopped=self._stopping),
                          exit_code=code)
        if self._headless:
            QApplication.quit()

    def _on_failed(self, message):
        self._set_running(False)
        self._close_entry(history_mod.ERROR, summary_suffix=message)
        if self._headless:
            logging.error("Run failed: %s", message)
            QApplication.quit()
            return
        widgets.warn(self, "Run", message)

    def _note_run_dir(self, where):
        """Write the run's directory into its history entry as soon as it is known."""
        if self._entry_id is not None and where:
            self.history.note(self._entry_id, run_dir=where)

    def _close_entry(self, status, exit_code=None, summary_suffix=""):
        """Record how the run ended, and keep a copy of its log.

        The log has to be archived now: the next run clears the Log page, so this
        is the only moment the text still exists anywhere.
        """
        if self._entry_id is None:
            return
        summary = self.run_state.summary or {}
        passed, total = self.run_state.totals()
        entry_id, self._entry_id = self._entry_id, None
        log_path = self.history.log_path(entry_id)
        fields = {"status": status, "exit_code": exit_code,
                  "passed": int(summary.get("passed", passed) or 0),
                  "total": int(summary.get("total", total) or 0),
                  "run_dir": self.run_state.run_dir,
                  "log_file": log_path if self.log.write_to(log_path) else ""}
        if summary_suffix:
            existing = (self.history.entry(entry_id) or {}).get("summary", "")
            fields["summary"] = "%s — %s" % (existing, summary_suffix) if existing \
                else summary_suffix
        self.history.finish(entry_id, **fields)

    def _set_running(self, running):
        self.run_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.run_action.setEnabled(not running)
        self.stop_action.setEnabled(running)
        self.command.set_running(running)
        self.launch.set_running(running)
        self.status_state.setText("state: running" if running else "state: idle")
        self._update_workers()   # the count is a live fact; it goes when the run does
        if running:
            self.status_right.setText("events: stdout · logs: stderr")

    # -- history --------------------------------------------------------------
    def restore_entry(self, entry):
        """Put a recorded run back into the page that produced it."""
        kind = entry.get("kind")
        if kind == history_mod.LAUNCH:
            self.launch.set_state(entry.get("launch_config") or {})
            self.show_page("launch")
            return "launch"
        # The Command page needs nothing new for this: set_state is its own API.
        if not self.settings.developer_mode:
            self.set_developer_mode(True)
        self.command.set_state(entry.get("command_state") or {})
        self.show_page("commands")
        return "commands"

    def rerun_entry(self, entry):
        source = self.restore_entry(entry)
        # Running in the background is part of what was asked for, even though no
        # saved configuration keeps it - the command it became does.
        self.start_run(source, background="--no-browser" in (entry.get("argv") or []))

    def _open_log(self, path):
        """Show an archived log on the Log page, where the filters are.

        Handing it to the desktop would open a text editor on something this app
        already renders better - level colouring, the session picker, search.
        """
        if not path or not os.path.isfile(path):
            return
        if self.log.show_file(path):
            self.show_page("log")
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _open_artifacts(self, run_dir):
        self.artifacts.show_dir(run_dir)
        self.show_page("artifacts")

    # -- chrome ---------------------------------------------------------------
    def _update_status(self):
        """Name the config file in play, without its full path."""
        path = self.core.config_path
        self.status_config.setText("config: %s"
                                   % (os.path.basename(path or "") or "—"))
        # The full path on the tooltip: this is an absolute one, and a status-bar
        # QLabel is as wide as its text, so spelling it out here would set the
        # window's minimum width to the length of it.
        self.status_config.setToolTip(path or "")

    def about(self):
        """What the application is, and which version of it this is.

        One application with one version: how it is split into processes inside
        is its own business, and not what someone opening About is asking.
        """
        widgets.note(self, "About QAVector", "QAVector", self.about_text())

    def about_text(self):
        return ("QAVector - Explore. Build. Verify.\n\n"
                "A desktop application for testing web applications end to end. "
                "It signs your accounts in, each in its own browser session, runs "
                "recorded scenarios across them, starts and watches the services "
                "they depend on, and keeps a report of every run.\n\n"
                "Version: %s\nQt (PySide6): %s" % (gui_version(), _pyside_version()))

    def _confirm_close_during_run(self):
        """Ask before closing on top of a live run. True means go ahead.

        A warning rather than a question, and the buttons say what they do:
        "Yes" and "No" leave it open which one closes without stopping, and that
        is the one outcome nobody wants - the launcher would be killed with its
        windows still up and their logins unflushed.
        """
        windows = len(self.run_state.sessions)
        return widgets.confirm(
            self, "A run is in progress", "Autotests are still running.",
            _close_warning(windows), agree="Stop all and close", kind="warn")

    def _aim_log_sources(self):
        """Tell the core which logsources.json is in force.

        The GUI edits it and the launcher reads it, so they have to be the same
        file or the page's own Open buttons read something else. Resolved here
        rather than in either of them: the setting wins, then the default under
        the user's own directory, and the launcher's own location is read while
        it is still the only one there - and until Save moves it, that is the one
        the core is pointed at too.
        """
        read_from, _target = lsf.resolve_path(self.settings.log_sources_path,
                                              self.core.legacy_log_sources)
        self.core.log_sources = read_from

    def _confirm_close_with_services(self):
        """Ask before closing on top of services that cannot outlive us.

        A service is either allowed to detach or it is not, and one that is not
        is a child of this process: PySide6 binds no ``setChildProcessModifier``,
        so there is no PR_SET_PDEATHSIG to catch it if this window goes first.
        Confirming here and stopping them below is the whole mechanism, which is
        why it is a warning and why the buttons say what they do. Services set to
        detach are not mentioned at all - nothing is about to happen to them.
        """
        doomed = self.services.detained()
        if not doomed:
            return True
        names = ", ".join("%s · %s" % (s.project, s.name) for s in doomed[:6])
        if len(doomed) > 6:
            names += ", and %d more" % (len(doomed) - 6)
        return widgets.confirm(
            self, "Services are running",
            "%d service(s) stop when this window closes." % len(doomed),
            "%s\n\nThey were not set to detach, so they cannot keep running "
            "without this application. Tick \"Detach allowed\" on a service to "
            "let it stay up." % names,
            agree="Stop them and close", kind="warn")

    def _update_services(self, *_args):
        """The footer's line about services: `app server: running`.

        Read off the supervisor rather than tallied here, so there is one answer
        to what is up and the page and the footer cannot disagree. Only what is
        actually running is named: a footer listing every configured service and
        its state would be the page again, in less room.
        """
        running = self.services.supervisor.running()
        if not running:
            self.services_label.setText("")
            self.services_label.setToolTip("")
            return
        names = ["%s: running" % service.name for service in running]
        shown = " · ".join(names[:SERVICES_IN_FOOTER])
        if len(names) > SERVICES_IN_FOOTER:
            shown += " · +%d" % (len(names) - SERVICES_IN_FOOTER)
        self.services_label.setText(shown)
        # The rest, with the project each belongs to - two services can share a
        # name across projects, and the footer has no room to say which is which.
        self.services_label.setToolTip("\n".join(
            "%s · %s: running" % (service.project, service.name)
            for service in running))

    def _schedule_nav_badges(self):
        """Coalesce the run's updates: RunState.changed fires on every event.

        A window launching, a step finishing, a line of log - all of them land
        here, and repainting the rail for each would be work done hundreds of
        times to arrive at the same two numbers.
        """
        if not self._badges_pending:
            self._badges_pending = True
            QTimer.singleShot(0, self._update_nav_badges)

    def _update_nav_badges(self, *_args):
        """The rail's live counts: services up and failed, windows still open.

        Read off the supervisor and the run state rather than tallied here, for
        the same reason the footer is: one answer to what is running, so the
        rail and the page it points at cannot disagree.
        """
        self._badges_pending = False
        services = self._nav_buttons.get("logsources")
        if services is not None:
            supervisor = self.services.supervisor
            up, down = len(supervisor.running()), len(supervisor.failed())
            services.set_badges(
                [(up, "ok"), (down, "bad")],
                " · ".join(filter(None, ["%d running" % up if up else "",
                                         "%d failed" % down if down else ""])))
        run = self._nav_buttons.get("run")
        if run is not None:
            live = len(self.run_state.live())
            run.set_badges([(live, "ok")],
                           "%d window%s open" % (live, "" if live == 1 else "s"))
        # A point may be all there is to see - a long label leaves no room for
        # the figure - so the numbers behind it always go on the tooltip, and
        # they change long after it was first set.
        for key in NAV_BADGES:
            if key in self._nav_buttons:
                self._nav_buttons[key].setToolTip(self._nav_tooltip(key))

    def _nav_tooltip(self, key):
        """What a rail entry says on hover: its counts, and its name if hidden.

        Expanded, only the counts. Beside a label that is right there, a tooltip
        repeating it is something to wait for that says nothing - but what a
        point stands for is not on the rail at all, whichever state it is in.
        """
        button = self._nav_buttons.get(key)
        counts = button.summary() if button is not None else ""
        if not self.settings.sidebar_collapsed:
            return counts
        label = self._nav_labels.get(key, "")
        return "%s\n%s" % (label, counts) if counts else label

    def closeEvent(self, event):
        if self.process.is_running():
            if not self._confirm_close_during_run():
                event.ignore()
                return
            self.status_right.setText("stopping the run before closing…")
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                self.process.stop()
                # Long enough for the launcher's own graceful teardown: it
                # SIGTERMs every window and gives Chrome up to 15 s each to flush
                # its cookies. Cutting that short is what loses a login.
                self.process._proc.waitForFinished(CLOSE_WAIT_MS)
            finally:
                QApplication.restoreOverrideCursor()
        # Asked before the services question: losing an edit is the one thing
        # closing can do that cannot be undone afterwards.
        if not self.services.confirm_discard():
            event.ignore()
            return
        if not self._confirm_close_with_services():
            event.ignore()
            return
        # Always, not only when something is running: shutdown also lets go of
        # any status probe still in flight, which Qt otherwise complains about
        # while the window is being destroyed underneath it.
        stopping = bool(self.services.detained())
        if stopping:
            self.status_right.setText("stopping services before closing…")
            QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.services.shutdown()
        finally:
            if stopping:
                QApplication.restoreOverrideCursor()
        self._closing = True
        self._metrics_timer.stop()
        self.settings.save_geometry(self.saveGeometry())
        # A QThread whose QObject is destroyed while it is still running takes the
        # process down with it, which turns closing the window mid-describe into a
        # crash on the way out. --describe is a subprocess we cannot cancel, so
        # wait for it rather than pretend it can be stopped.
        if self.loader_thread is not None and self.loader_thread.isRunning():
            self.loader_thread.wait(5000)
        super().closeEvent(event)


def _pyside_version():
    try:
        import PySide6
        return PySide6.__version__
    except Exception:
        return "?"
