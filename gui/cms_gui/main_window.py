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
from types import SimpleNamespace

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QFrame, QHBoxLayout,
                               QLabel,
                               QMainWindow, QMenu,
                               QPushButton, QSizePolicy, QStackedWidget,
                               QToolButton, QVBoxLayout, QWidget)

from . import (background, commands, core as core_mod, cycleinteractions, cyclereplay,
               cyclelayoutfile as clf, cycleprojectsfile as cpf, history as history_mod,
               icon, icons, projectsearch, projectsfile,
               launch as launch_mod, launchprojectsfile as lpf, load as load_mod,
               logsourcesfile as lsf, search, stageswindow, subjecthistory, theme,
               titlebar, transfer, usersfile,
               widgets)
from .askbridge import AskBridge
from .cyclecockpit import ActiveStrip
from .cyclewatch import Listener
from .cycleschedule import PlanScheduler
from .loader import LoaderThread
from .hotkeys import ActionRegistry, ActionSpec, bind_tooltip
from . import version as gui_version
from .runner import LauncherProcess, RunState
from .servicebridge import ServiceBridge
from .settings import Settings
from .thirdparty import AboutDialog
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
from .pages.subjects import SubjectsPage
from .pages.settings_dialog import SettingsDialog
from .pages.transferdialogs import ExportDialog, ImportDialog

# (page key, label, icon name painted by icons.icon)
CONFIGURE = [("environments", "Environments", "environments"),
             ("credentials", "Credentials", "credentials"),
             ("logsources", "Services & Logs", "services"),
             ("scenarios", "Scenarios", "scenarios"),
             ("cycles", "Cycles", "cycles"),
             ("commands", "Command", "command"),
             ("launch", "Launch Sessions", "launch")]
# Run always heads Observe - what is going on now comes before everything about
# what went on. A page added to the group goes after it.
OBSERVE = [("run", "Run", "run"), ("subjects", "Subjects", "subjects"),
           ("log", "Log", "log"), ("artifacts", "Artifacts", "artifacts"),
           ("history", "History", "history")]

# Pages only a developer needs. Launch Sessions is never in here: it is the
# primary interface in one mode and still available in the other.
DEVELOPER_ONLY = ("commands",)

# How many live counts a rail entry can show: Services & Logs says how many are
# running and how many have failed, Run says how many windows are up. Every
# entry reserves room for the larger of these whether or not it uses it, so the
# points line up in one column and the marks stay aligned when the rail is
# collapsed - see widgets.NavButton.
NAV_BADGES = {"logsources": 2, "cycles": 2, "run": 1}

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


def _running_and_failed(up, down):
    """A rail entry's two counts in words, for its tooltip: Services and Cycles."""
    return " · ".join(filter(None, ["%d running" % up if up else "",
                                    "%d failed" % down if down else ""]))


# How a run on emptied profiles announces itself, on the Run page and in History.
EMPTY_PREFIX = "empty sessions · "


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
        #: Whether the last cycle run was put back on its page yet - see
        #: refresh_inventory. Once per window, not once per refresh.
        self._last_cycle_restored = False
        self._auto_launch = auto_launch
        self._headless = headless
        self._closing = False
        self.setWindowTitle("QAVector")
        self.setWindowIcon(icon.app_icon())
        self.resize(1380, 880)

        self.settings = Settings()
        self.action_registry = ActionRegistry(self.settings, self)
        # The project every page shows: its files, from the start.
        self._adopt_launch_projects()
        self._use_project(self._project(self.settings.current_project))
        self.core = self._new_core()
        self._aim_log_sources()
        self.inventory = core_mod.Inventory()
        self.run_state = RunState(self)
        self.process = LauncherProcess(self)
        self.history = history_mod.History(self.settings.data_dir(), parent=self)
        self._project_name_at_start = self._project(self.settings.project_id).name
        #: Called once the inventory of a project just switched to has loaded.
        self._after_switch = None

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
        #: The run being parked: paused, and stopped once nothing but a question
        #: to a person is still going in it (see park_cycle_run).
        self._parking = ""
        self._chrome_warned = False
        self._build_menu()
        self._build_ui()
        self.command.state_changed.connect(self._update_background_action)
        self.launch.state_changed.connect(self._update_background_action)
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
        # Beside it, because it is the same kind of fact - something running
        # in the background: whether any cycle is listened for, when it next
        # asks, or that its last check failed, which is otherwise silent.
        self.listen_label = QLabel("")
        self.listen_label.setStyleSheet("padding: 0 10px;")
        self.statusBar().addPermanentWidget(self.listen_label)
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
        # And the Cockpits list what it holds open, and bring a question back.
        self.ask_bridge.changed.connect(self._questions_changed)
        self.cycles.question_requested.connect(self.ask_bridge.show)
        self.subjects_page.question_requested.connect(self.ask_bridge.show)

        self.workers_label = QLabel("")
        self.workers_label.setStyleSheet("padding: 0 10px;")
        self.statusBar().addPermanentWidget(self.workers_label)
        self.run_state.changed.connect(self._update_workers)
        # Placed last of all, at the end of __init__: see _place_metrics.
        self.metrics_label = QLabel("")
        self.metrics_label.setStyleSheet("padding: 0 10px;")

        self.loader_thread = None
        #: A refresh asked for while one was running - see refresh_inventory.
        self._inventory_stale = False
        # New tasks in the queues cycles watch. Asked about one at a time, and
        # only when nothing is running - see _offer_next.
        self.listener = Listener(self.core, self)
        self.listener.offers_changed.connect(self._offer_next)
        self._offering = False
        # Search, Ctrl+K from any window. No button and no menu entry: the key
        # is the way in - an action on the window and in no menu. Text fields
        # let the key through to it (search.KeyCatcher, attached at startup).
        self.cycles.set_listener(self.listener)
        self._search = None
        self.search_action = QAction("Search", self)
        self.search_action.setShortcut(QKeySequence(search.SHORTCUT))
        self.search_action.setShortcutContext(Qt.ApplicationShortcut)
        self.search_action.triggered.connect(self.open_search)
        self.addAction(self.search_action)
        self._register_action(self.search_action, "application.search", "Search",
                              "Search records across the application.", "Application")
        self.action_registry.changed.connect(self._refresh_hotkey_hints)
        self._refresh_hotkey_hints()
        # Planned entries somebody asked to start later. Started when their
        # time comes - after the run going on then, if there is one.
        self.scheduler = PlanScheduler(self)
        self.scheduler.due.connect(self._scheduled_due)
        # Here rather than in _build_ui, which runs before the scheduler exists.
        self.cycles.planned_loaded.connect(self.scheduler.set_planned)
        self._due = []
        self._listen_timer = QTimer(self)
        self._listen_timer.timeout.connect(self._update_listening)
        self._listen_timer.start(1000)
        self._load = load_mod.Sampler()
        self._metrics_timer = QTimer(self)
        self._metrics_timer.timeout.connect(self._update_metrics)
        self._metrics_timer.start(2000)
        
        self._place_metrics()

        # Ask the core what exists as soon as the window is up, so the pages are
        # populated before the user reaches them.
        QTimer.singleShot(500 if self.splash else 80, self.refresh_inventory)
        if self.splash:
            QTimer.singleShot(SPLASH_MAX_MS, self._finish_splash)

    # -- construction ---------------------------------------------------------
    def _register_action(self, action, action_id, name, description, category,
                         scope="application"):
        """The same QAction serves its menu, buttons and configured shortcut."""
        if scope == "application" and action.shortcutContext() != Qt.ApplicationShortcut:
            scope = "component/main_window"
        self.action_registry.register(ActionSpec(
            action_id, name, description, category,
            action.shortcut().toString(QKeySequence.PortableText), scope), action)
        return action

    def _build_menu(self):
        menu = self.menuBar()

        file_menu = menu.addMenu("&File")
        import_action = QAction("&Import…", self)
        import_action.triggered.connect(lambda: self.import_items())
        self._register_action(import_action, "transfer.import", "Import",
                              "Import projects, environments, users or cycles from a "
                              "file.", "Application")
        file_menu.addAction(import_action)
        export_action = QAction("&Export…", self)
        export_action.triggered.connect(lambda: self.export_items())
        self._register_action(export_action, "transfer.export", "Export",
                              "Export projects, environments, users or cycles to a "
                              "file.", "Application")
        file_menu.addAction(export_action)
        file_menu.addSeparator()
        # Not in the menu: Credentials has its own Reload and Save, and these
        # are their keys (Ctrl+R, Ctrl+S) on that page.
        reload_action = QAction("&Reload users.json", self)
        reload_action.setShortcut(QKeySequence("Ctrl+R"))
        reload_action.triggered.connect(lambda: self.credentials.reload(self.core.config_path))
        self.reload_users_action = self._register_action(
            reload_action, "credentials.reload", "Reload users.json",
            "Reload the credentials file.", "Credentials", "page/credentials")
        save_action = QAction("&Save users.json", self)
        save_action.setShortcut(QKeySequence.Save)
        save_action.triggered.connect(lambda: self.credentials.save_button.click())
        self.save_users_action = self._register_action(
            save_action, "credentials.save", "Save users.json",
            "Save the credentials file.", "Credentials", "page/credentials")
        settings_action = QAction("Se&ttings…", self)
        settings_action.triggered.connect(self.open_settings)
        self.settings_action = self._register_action(
            settings_action, "application.settings", "Settings",
            "Open application settings.", "Application")
        file_menu.addAction(settings_action)
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        self._register_action(quit_action, "application.quit", "Quit",
                              "Close QAVector.", "Application")
        file_menu.addAction(quit_action)

        run_menu = menu.addMenu("&Run")
        #: The menu bar's Run: the RUN button's three ways of launching (put in
        #: once the toolbar has made them), the last cycle again, Stop.
        self.menu_run = run_menu
        self.last_cycle_action = QAction("Run &Last Cycle", self)
        self.last_cycle_action.triggered.connect(self.run_last_cycle)
        self._register_action(self.last_cycle_action, "run.last_cycle", "Run Last Cycle",
                              "Run again the cycle that ran last.", "Run")
        run_menu.addAction(self.last_cycle_action)
        self._run_menu_gap = run_menu.addSeparator()
        run_menu.aboutToShow.connect(self._update_run_menu)
        self.run_action = QAction("&Run command", self)
        self.run_action.setShortcut(QKeySequence("Ctrl+Return"))
        self.run_action.triggered.connect(self.start_run)
        self._register_action(self.run_action, "run.start", "Run command",
                              "Start the current launch configuration.", "Run")
        # The RUN button and Ctrl+Return, not a menu item: the menu said "Run
        # command" for what is a launch configuration or a cycle.
        self.addAction(self.run_action)
        self.stop_action = QAction("&Stop", self)
        self.stop_action.setShortcut(QKeySequence("Ctrl+."))
        self.stop_action.triggered.connect(self.stop_run)
        self.stop_action.setEnabled(False)
        self._register_action(self.stop_action, "run.stop", "Stop",
                              "Stop the running launch or cycle.", "Run")
        run_menu.addAction(self.stop_action)
        # From any window of the application, not only this one: somebody
        # notices the work heading the wrong way while reading a log window
        # or another page, and that is where they should be able to say so.
        # (A modal dialog still keeps the keys to itself, as Qt has it.)
        self.clarify_action = QAction("&Clarify the running cycle\u2026", self)
        self.clarify_action.setShortcut(QKeySequence(cycleinteractions.SHORTCUT))
        self.clarify_action.setShortcutContext(Qt.ApplicationShortcut)
        self.clarify_action.triggered.connect(self.open_clarify)
        self._register_action(self.clarify_action, "cycle.clarify", "Clarify the running cycle",
                              "Send a clarification to the running cycle.", "Run")
        # Beside Run Last Cycle: both are about a cycle, Stop is about any run.
        run_menu.insertAction(self._run_menu_gap, self.clarify_action)
        self._clarify = None

        view_menu = menu.addMenu("&View")
        self.developer_action = QAction("&Developer mode", self)
        self.developer_action.setCheckable(True)
        self.developer_action.setShortcut(QKeySequence("Ctrl+Shift+D"))
        self.developer_action.toggled.connect(self.set_developer_mode)
        self._register_action(self.developer_action, "view.developer_mode", "Developer mode",
                              "Show developer tools and the Command page.", "View")
        view_menu.addAction(self.developer_action)
        
        self.always_on_top_action = QAction("Always on Top", self)
        self.always_on_top_action.setCheckable(True)
        self.always_on_top_action.toggled.connect(self.set_always_on_top)
        self._register_action(self.always_on_top_action, "view.always_on_top", "Always on Top",
                              "Keep QAVector above other windows.", "View")
        view_menu.addAction(self.always_on_top_action)
        
        self.dark_mode_action = QAction("&Dark mode", self)
        self.dark_mode_action.setCheckable(True)
        self.dark_mode_action.setChecked(self.settings.dark_mode)
        self.dark_mode_action.toggled.connect(self.set_dark_mode)
        self._register_action(self.dark_mode_action, "view.dark_mode", "Dark mode",
                              "Switch the application colour theme.", "View")
        view_menu.addAction(self.dark_mode_action)

        view_menu.addSeparator()
        # Where a cycle's stages are, and the way to them from anywhere. Kept in
        # step with the page's own icon both ways - see _stages_detached_changed.
        self.stages_window_action = QAction("Stages in a separate &window", self)
        self.stages_window_action.setCheckable(True)
        self.stages_window_action.toggled.connect(self.set_stages_detached)
        self._register_action(self.stages_window_action, "view.stages_window", "Stages in a separate window",
                              "Detach or dock the Stages panel.", "View")
        view_menu.addAction(self.stages_window_action)
        # From any window of the application, the detached Stages included: a
        # person reading a log or another page and wanting the run's account
        # should not first have to find the window it is in.
        self.go_stages_action = QAction("Go to &Stages", self)
        self.go_stages_action.setShortcut(QKeySequence(stageswindow.SHORTCUT))
        self.go_stages_action.setShortcutContext(Qt.ApplicationShortcut)
        self.go_stages_action.triggered.connect(self.go_to_stages)
        self._register_action(self.go_stages_action, "view.go_to_stages", "Go to Stages",
                              "Bring the cycle's Stages panel forward.", "View")
        view_menu.addAction(self.go_stages_action)
        self._stages_restored = False

        view_menu.addSeparator()
        self.collapse_action = QAction("&Collapse sidebar", self)
        self.collapse_action.setCheckable(True)
        self.collapse_action.setShortcut(QKeySequence("Ctrl+B"))
        self.collapse_action.toggled.connect(self.set_sidebar_collapsed)
        self._register_action(self.collapse_action, "view.collapse_sidebar", "Collapse sidebar",
                              "Collapse or expand the navigation sidebar.", "View")
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
            self._register_action(action, "view.sidebar." + key,
                                  "Show sidebar item: " + label,
                                  "Show or hide this page in the sidebar.", "View")

        tools_menu = menu.addMenu("&Tools")
        refresh_action = QAction(self)
        refresh_action.setShortcut(QKeySequence("F5"))
        refresh_action.triggered.connect(self.refresh_inventory)
        self.wording.text(refresh_action, "Refresh", "Refresh --describe")
        self.refresh_action = self._register_action(
            refresh_action, "application.refresh", "Refresh",
            "Reload the inventory from the core.", "Application")
        tools_menu.addAction(refresh_action)
        tools_menu.addSeparator()
        for plain, spelled_out, args in commands.ONE_SHOTS:
            action = QAction(self)
            # The window it opens is titled with whichever name the menu used, so
            # the two never disagree about what was asked for.
            action.triggered.connect(
                lambda _c=False, a=args, act=action: self.one_shot(act.text(), a))
            self.wording.text(action, plain, spelled_out)
            self._register_action(action, "tools." + args[0].lstrip("-").replace("=", "."),
                                  plain, "Run " + " ".join(args) + ".", "Tools")
            tools_menu.addAction(action)
        tools_menu.addSeparator()
        init_action = QAction("Create a starter users.json", self)
        init_action.triggered.connect(
            lambda: self.one_shot("Create a starter users.json",
                                  ["--init-users-json"]))
        self._register_action(init_action, "tools.init_users", "Create a starter users.json",
                              "Create an initial credentials file.", "Tools")
        tools_menu.addAction(init_action)

        help_menu = menu.addMenu("&Help")
        about_action = QAction("About", self)
        about_action.triggered.connect(self.about)
        self._register_action(about_action, "application.about", "About",
                              "Show version and third-party licences.", "Application")
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
        self.run_button.clicked.connect(self.run_action.trigger)
        bind_tooltip(self.run_action, self.run_button, "Run the current launch configuration")
        self.run_menu = QMenu(self.run_button)
        # The tooltips carry the reason an entry is greyed out, and a menu keeps
        # its tooltips to itself unless told otherwise.
        self.run_menu.setToolTipsVisible(True)
        # The same launch with no browser at all. Offered only for scenarios that
        # need none, which is decided as the menu opens, from what RUN would run
        # at that moment - see _update_background_action.
        self.background_action = QAction("In Background", self)
        self.background_action.triggered.connect(self.start_background_run)
        self._register_action(self.background_action, "run.background", "Run in Background",
                              "Run service scenarios without a browser.", "Run")
        self.run_menu.addAction(self.background_action)
        # Whether recording continues a scenario or starts a new one follows from
        # what is selected, and is confirmed rather than guessed.
        self.record_action = QAction("With Recorder", self)
        self.record_action.setToolTip(
            "Open the windows with the Scenario Recorder shown in each of them. "
            "Continues the selected scenario if there is one.")
        self.record_action.triggered.connect(self.start_recording)
        self._register_action(self.record_action, "run.record", "Run with Recorder",
                              "Launch sessions with the scenario recorder.", "Run")
        self.run_menu.addAction(self.record_action)
        # The same launch again, on profiles emptied first. Always offered: any
        # run that opens windows can start from empty ones.
        self.empty_action = QAction("Empty Sessions", self)
        self.empty_action.setToolTip(
            "Open every window with its cache, cookies, site storage and history "
            "cleared first; each account signs in from scratch. The profile "
            "folders and their extensions stay.")
        self.empty_action.triggered.connect(self.start_empty_run)
        self._register_action(self.empty_action, "run.empty_sessions", "Run Empty Sessions",
                              "Launch sessions after clearing their browser state.", "Run")
        self.run_menu.addAction(self.empty_action)
        self.run_menu.aboutToShow.connect(self._update_background_action)
        self.run_button.setMenu(self.run_menu)
        # The same three in the menu bar's Run, above Run Last Cycle.
        self.menu_run.insertActions(self.last_cycle_action, [
            self.background_action, self.record_action, self.empty_action])
        self.menu_run.insertSeparator(self.last_cycle_action)
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
        self.stop_button.clicked.connect(self.stop_action.trigger)
        bind_tooltip(self.stop_action, self.stop_button, "Stop the current run")
        self.stop_menu = QMenu(self.stop_button)
        # Filled from the live run: which windows exist is not knowable until one
        # is running, and stale entries would offer to stop what is already gone.
        self.stop_menu.aboutToShow.connect(self._fill_stop_menu)
        self.stop_button.setMenu(self.stop_menu)
        # Beside Settings, as an icon: reading everything again is a thing done
        # now and then, not the second most important thing in the window.
        self.refresh_button = icons.button(QPushButton(""), "refresh")
        self.refresh_button.setProperty("variant", "ghost")
        self.refresh_button.clicked.connect(self.refresh_action.trigger)
        bind_tooltip(self.refresh_action, self.refresh_button, "Refresh the inventory")
        # The project is what every page shows, so it is the window's: picked
        # here, its folder is where every page reads and writes.
        self.project_combo = widgets.capped(QComboBox())
        self.project_combo.setMinimumWidth(200)
        self.project_combo.setPlaceholderText("Project")
        self.project_combo.setToolTip("The project: every page shows its data")
        self.project_combo.activated.connect(self._project_picked)
        self._fill_projects()
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
        settings_button.clicked.connect(self.settings_action.trigger)
        bind_tooltip(self.settings_action, settings_button, "Open settings")
        bar_layout.addWidget(widgets.row(
            self.run_button, self.stop_button, widgets.vline(),
            self.project_combo, None, self.describe_label, widgets.vline(),
            self.developer_button, self.refresh_button, settings_button))
        column.addWidget(bar)
        self.cycle_strip = ActiveStrip(self.run_state, self.settings, self,
                                       navigation_needed=self._cycle_navigation_needed)
        self.cycle_strip.opened.connect(self._show_cycle_cockpit)
        column.addWidget(self.cycle_strip)
        self._supervision_timer = QTimer(self)
        self._supervision_timer.timeout.connect(self._confirm_cycle_supervision)
        self._supervision_timer.start(1000)

        # --- body: rail + pages ---------------------------------------------
        body = QWidget()
        body_row = QVBoxLayout(body)
        body_row.setContentsMargins(0, 0, 0, 0)
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
        self.collapse_button.clicked.connect(self.collapse_action.trigger)
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
        # No versions at its foot: they are in Help -> About and Settings, and a
        # rail is for going places.
        inner.addWidget(rail)

        self.stack = QStackedWidget()
        # Over the pages while a project just picked is being read: they show
        # nothing of the one left, and cannot be acted on half-loaded.
        self.switching = widgets.Busy(self.stack)
        # One reading of launch-projects.json for the three pages that use it:
        # where an environment opens is set on one and shown on the others.
        self.launch_registry = lpf.Registry(
            lpf.resolve_path(self.settings.launch_projects_path))
        self.environments = EnvironmentsPage(self.settings, registry=self.launch_registry)
        self.credentials = CredentialsPage()
        self.services = ServicesPage(self.settings)
        self.services.set_core(self.core)
        self.scenarios = ScenariosPage(self.settings)
        self.cycles = CyclesPage(self.settings)
        self.cycles.set_run_state(self.run_state)
        self.cycles.interaction_requested.connect(self._send_cycle_interaction)
        self.cycles.cycle_opened.connect(self._restore_selected_cycle)
        self.cycles.session_opened.connect(self._open_cycle_run)
        self.cycles.planned_started.connect(self._start_planned)
        self.cycles.planned_opened.connect(self._open_planned)
        self.cycles.artifacts_requested.connect(self._open_artifacts)
        self.cycles.stages_detached_changed.connect(self._stages_detached_changed)
        self.command = CommandPage(self.settings, registry=self.launch_registry)
        self.launch = LaunchSessionsPage(self.settings, registry=self.launch_registry)
        self.launch.project_name = self._project_name_at_start
        # A desktop link's run is a windowless copy of the application; it must
        # not decide what the next ordinary start opens on.
        self.launch.set_persistent(not self._headless)
        self.run = RunPage(self.run_state)
        self.log = LogPage(self.settings)
        self.artifacts = ArtifactsPage(self.settings)
        self.history_page = HistoryPage(self.history)
        self.subjects_page = SubjectsPage(self.settings)
        self.subjects_page.set_core(self.core)
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
                       "artifacts": self.artifacts, "history": self.history_page,
                       "subjects": self.subjects_page}
        for page in self._pages.values():
            self.stack.addWidget(page)
        # Credentials and cycle documents may both use Save, in their own page.
        # The menu keeps the same action and handler as its page's controls.
        for action in (self.reload_users_action, self.save_users_action):
            action.setParent(self.credentials)
            action.setShortcutContext(Qt.WidgetWithChildrenShortcut)
            self.credentials.addAction(action)
        bind_tooltip(self.reload_users_action, self.credentials.reload_button, "Reload users.json")
        bind_tooltip(self.save_users_action, self.credentials.save_button, "Save users.json")
        self._page_actions = {}
        for key, label, _mark in CONFIGURE + OBSERVE:
            action = QAction("Go to " + label, self)
            action.triggered.connect(lambda _checked=False, k=key: self.show_page(k))
            self.addAction(action)
            self._register_action(action, "navigate." + key, "Go to " + label,
                                  "Open the " + label + " page.", "Navigation")
            self._page_actions[key] = action
            self._nav_buttons[key].clicked.disconnect()
            self._nav_buttons[key].clicked.connect(action.trigger)
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
        # What a write answered with, already in the page's own list: only
        # what else reads the cycles needs telling, not a whole --describe.
        self.cycles.cycles_changed.connect(self._cycles_changed)
        self.cycles.run_requested.connect(self._start_page_cycle_run)
        self.cycles.stop_requested.connect(self.stop_run)
        self.cycles.control_requested.connect(self._control_cycle)
        self.cycles.approval_answered.connect(self._answer_cycle_node_approval)
        self.environments.addresses_changed.connect(self._addresses_changed)
        self.launch.environments_changed.connect(self._launch_environments_changed)
        # + Account wrote users.json, or the Environments page did (who is in
        # an environment): read it again, as a Credentials save does.
        self.launch.accounts_changed.connect(self.refresh_inventory)
        self.environments.accounts_changed.connect(self.refresh_inventory)
        # Projects are managed on the Environments page, picked in the top bar.
        self.environments.project_action.connect(self._project_action)
        # Export from a page opens File -> Export with what it shows ticked.
        self.environments.export_requested.connect(self.export_items)
        self.credentials.export_requested.connect(self.export_items)
        self.cycles.export_requested.connect(self.export_items)
        self.cycles.import_requested.connect(lambda: self.import_items())
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
        self.subjects_page.open_run_requested.connect(self._open_subject_run)
        self.subjects_page.resume_requested.connect(self._resume_subject_run)
        self.subjects_page.park_requested.connect(self.park_cycle_run)
        # Each of these is shown here, so here comes forward - a subject's
        # section in a window of its own would otherwise stay in front of it.
        # Answered in a subject's own Cockpit, as in the Cycles page's: the same
        # receipts, the same control command, the same standing questions.
        self.subjects_page.set_run_state(self.run_state)
        self.subjects_page.approval_answered.connect(self._answer_cycle_node_approval)
        self.subjects_page.step_requested.connect(self._show_subject_step)
        self.subjects_page.artifact_requested.connect(
            lambda run_dir, path: (self.come_forward(), self._open_artifacts(run_dir, path)))
        self.subjects_page.run_on_requested.connect(self._run_on_subject)
        # The live Stages panel is lent to a subject's row, never copied: the
        # Cycles page owns it and has it back whenever it asks.
        self.subjects_page.set_stages_lender(SimpleNamespace(
            lend=self.cycles.lend_stages, take_back=self.cycles.take_back_stages,
            detached=self.cycles.is_stages_detached, show_window=self.cycles.show_stages))
        self.cycles.subject_requested.connect(self._show_subject)
        # What a run is doing decides what an open subject offers to do next,
        # and a run that moved has changed where its subject stands.
        self.run_state.changed.connect(self._subjects_follow)
        self.run_state.cycle_subject_changed.connect(self._subjects_follow)
        self.run_state.cycle_step_changed.connect(lambda _step: self._subjects_follow())

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
        # A page's own keys - Save and Reload users.json, a cycle's Save and
        # Undo, Find - work where the focus is, and the rail does not take it.
        # Left on the toolbar, or nowhere after a dialog closed, it kept them
        # from the page on show until somebody clicked into it.
        focused = QApplication.focusWidget()
        if focused is None or not (focused is page or page.isAncestorOf(focused)):
            page.setFocus(Qt.OtherFocusReason)
        for name, button in self._nav_buttons.items():
            button.setProperty("active", "true" if name == key else "false")
            button.style().unpolish(button)
            button.style().polish(button)
        self._page_key = key
        self.cycle_strip.refresh()
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
            if key in getattr(self, "_page_actions", {}):
                self._page_actions[key].setEnabled(self._offered(key, hidden))
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
        icons.button(self.collapse_button, "expand" if collapsed else "collapse")
        self._refresh_hotkey_hints()
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

    def _place_metrics(self):
        """CPU and RAM go at the far right of the status bar, always.

        The status bar lays permanent widgets out in the order they are added,
        so whatever is added after this one lands to its right. Added here, as
        the last thing construction does, rather than beside its creation - a
        label added later in __init__ once pushed "Listening" past it.
        """
        self.statusBar().addPermanentWidget(self.metrics_label)

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
        self._update_background_action()

    def _run_page(self, source=None):
        source = source or self._run_source
        if source == "commands" and self.settings.developer_mode:
            return "commands", self.command
        return "launch", self.launch

    def _run_auto_launch(self):
        """Run the configuration a desktop link named (``--launch NAME``).

        The configuration as saved: a link names a configuration, not whatever
        edit of it - or of another one entirely - was last on screen. A name
        nothing is saved under runs nothing.
        """
        name, self._auto_launch = self._auto_launch, None
        self.show_page("launch")
        if not self.launch.open_configuration(name, use_draft=False):
            candidates = self.launch.link_candidates(name)
            if candidates:
                logging.error("The desktop link names %r, which %d saved configurations "
                              "share (%s); recreate the link from the one you mean.",
                              name, len(candidates), ", ".join(candidates))
            else:
                logging.error("No saved Launch Sessions configuration is called %r.",
                              name)
            if self._headless:
                QApplication.quit()
            return
        QTimer.singleShot(100, self.launch.run_requested.emit)

    def _launch_environments_changed(self):
        """Launch Sessions changed an environment: the other two pages show it."""
        self.environments.set_inventory_rows()
        self.command.refresh_address()

    def _addresses_changed(self):
        """An environment changed: both pages show where a run opens, and
        Credentials what it is called."""
        self.launch.refresh_address()
        self.command.refresh_address()
        if self.inventory is not None:
            self.credentials.set_environments(
                self.launch_registry.environments(self.inventory))

    # -- core -----------------------------------------------------------------
    def refresh_inventory(self):
        # The first refresh is a delayed singleShot, so a window closed inside
        # that delay would otherwise start a describe on its way out - a thread
        # nobody is left to wait for, and widgets nobody is left to fill in.
        if self._closing:
            return
        if self.loader_thread and self.loader_thread.isRunning():
            # Asked while a read is still going: that read may have taken the
            # files before this change was written, so one more follows it -
            # dropping the request left the tree showing the older state.
            self._inventory_stale = True
            return
        self._inventory_stale = False

        self.command.set_core(self.core)
        self.launch.set_core(self.core)
        # Scenarios reads and writes files through the core too, not just runs it.
        self.scenarios.set_core(self.core)
        self.cycles.set_core(self.core)
        self.services.set_core(self.core)
        
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
            self.switching.stop()
            self.cycles.inventory_loading(False)
            self.describe_label.setText("could not read the configuration")
            self.status_right.setText("the core did not answer")
            self._finish_splash()
            if self._headless:
                logging.error("Cannot read the core: %s", exc)
                QApplication.quit()
                return
            widgets.warn(self, "Cannot read the core",
                                "%s" % exc)
                                
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
            self.cycles.inventory_loading(False)
            self.listener.set_core(self.core)
            self.listener.set_inventory(self.inventory)
            
            _splash_msg("Loading credentials...")
            self.credentials.set_environments(
                self.launch_registry.environments(self.inventory))
            # refresh, not load: an edit not saved yet stays where it is.
            self.credentials.refresh(self.inventory.config_path or self.core.config_path)
            self.launch.users_path = self.inventory.config_path or self.core.config_path

            _splash_msg("Loading services and logs...")
            self.services.set_environments([env.get("value", "")
                                            for env in self.inventory.envs])
            # Another project's log sources are its own file: never the old
            # one beside the launcher, which is Workspace's from before.
            self.services.load(self.settings.log_sources_path
                               if self.settings.project_folder
                               else (self.core.legacy_log_sources
                                     or self.inventory.log_sources_path),
                               self.settings.services_path,
                               self.settings.log_sources_path)
            # Loading replaces the supervisor's whole set, and a service that
            # simply stopped existing reports no status on its way out - so the
            # window's two tallies have to be asked again rather than waiting to
            # be told, or they go on naming what is no longer configured.
            self._update_services()
            self._update_nav_badges()

            self.switching.stop()
            # Search showing a row of the project just switched to.
            if self._after_switch is not None:
                then, self._after_switch = self._after_switch, None
                QTimer.singleShot(0, then)

            _splash_msg("Finalizing UI...")
            self.describe_label.setText(self.inventory.summary())
            # A project with no users.json yet is a new one, not a problem.
            warnings = [one for one in self.inventory.warnings
                        if not (self.settings.project_folder
                                and not os.path.exists(self.settings.config)
                                and one.startswith("Could not read config"))]
            if warnings:
                self.status_right.setText(warnings[0][:90])
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
            # Once, at startup: every save refreshes the inventory, and doing
            # it each time took whoever had just saved one cycle to the cycle
            # of the last run instead.
            if not self._last_cycle_restored:
                self._last_cycle_restored = True
                QTimer.singleShot(0, self._restore_last_cycle)

            if self._auto_launch:
                self._run_auto_launch()
                
        self.describe_label.setText("reading the configuration …")
        # The list of cycles is part of what this reads; while there is none
        # yet, the explorer shows that it is coming rather than that it is empty.
        self.cycles.inventory_loading(True)
        self.loader_thread = LoaderThread(self.core, self)
        self.loader_thread.progress.connect(_splash_msg)
        self.loader_thread.error.connect(_on_error)
        self.loader_thread.finished_inventory.connect(_on_finished)
        self.loader_thread.finished.connect(self._inventory_read)
        self.loader_thread.start()

    def _inventory_read(self):
        """The read thread is done: read again if somebody asked meanwhile."""
        if self._inventory_stale and not self._closing:
            self._inventory_stale = False
            QTimer.singleShot(0, self.refresh_inventory)

    def _cycles_changed(self, rows):
        """The Cycles page wrote cycles and holds their rows: tell the rest.

        The listener builds its triggers from the inventory's cycles, so the
        inventory is brought up to date in place rather than read again.
        """
        if self.inventory is None:
            return
        self.inventory.payload["cycles"] = list(rows)
        self.listener.set_inventory(self.inventory)

    def _restore_selected_cycle(self, cycle_id):
        """The cycle's last run, read back when the cycle is opened.

        The stream is read and parsed on a thread - a long one took a quarter
        of a second on this one - and fed to the model here when it arrives;
        the page paints it when the model says so.
        """
        state = self.run_state
        if (self.process.is_running() or state.cycle_running
                or (state.cycle or {}).get("cycle") == cycle_id):
            return
        where = cyclereplay.last_run_dir(self.history, cycle_id)
        if where:
            self._replay(where)

    def _replay(self, run_dir, then=None):
        """Read ``run_dir``'s events off the GUI thread, then feed them in."""
        def done(events, error):
            if error is not None or self.process.is_running() or self.run_state.cycle_running:
                self.cycles.show_loading(False)
                return
            restored = cyclereplay.restore(run_dir, self.run_state, events=events or [])
            if then is not None:
                then(restored)

        work = lambda: cyclereplay.events_of(run_dir)  # noqa: E731
        if background.offloads(self.core):
            background.run(work, done, owner=self)
        else:
            done(*background.call(work))

    def _open_cycle_run(self, cycle_id, run_dir, then=None):
        """Put one earlier run on the Cycles page - a session picked on the right.

        Replayed first and opened after, the way _restore_last_cycle does it:
        opening a cycle paints whatever the model holds, so the model has to
        hold the run before the canvas asks. The canvas and the output show
        their skeletons while the run is read. ``then`` runs once it is drawn.
        """
        if self.process.is_running() or not run_dir:
            return
        self.cycles.show_loading(True)

        def opened(restored):
            if restored:
                self.cycles.open(cycle_id, then=then)
            else:
                self.cycles.show_loading(False)

        self._replay(run_dir, opened)

    # -- Subjects: going back to a subject, and putting one aside -------------------
    def _subjects_follow(self):
        """Tell the Subjects page what this window is running, and that it moved."""
        page = getattr(self, "subjects_page", None)
        if page is None:
            return
        cycle = self.run_state.cycle or {}
        live = cycle.get("run_id", "") if self.run_state.cycle_running else ""
        page.set_live(live, self.process.is_running())
        page.refresh_soon()
        self._park_if_ready()

    def _show_subject(self, subject_id):
        """The Subjects page, opened on one subject - from the Cycles sidebar."""
        self.show_page("subjects")
        self.subjects_page.reveal(subject_id)

    def _open_subject_run(self, cycle_id, run_dir):
        """A subject's run on the Cycles page: the live one as it is, any other
        read back from its directory first."""
        cycle = self.run_state.cycle or {}
        if (self.run_state.cycle_running and run_dir
                and os.path.basename(os.path.normpath(run_dir)) == cycle.get("run_id")):
            self.come_forward()
            self.show_page("cycles")
            if self.cycles.current_id() != cycle_id:
                self.cycles.open(cycle_id)
            return
        if self.process.is_running():
            widgets.note(self._asked_from(), "Subjects",
                         "A run is going in this window. Another run can be opened once "
                         "it has finished, or after you park or stop it.")
            return
        self.come_forward()
        self.show_page("cycles")
        self._open_cycle_run(cycle_id, run_dir)

    def _show_subject_step(self, cycle_id, run_dir, run_id, step_id):
        """A step clicked in a subject's Cockpit, on the Cycles canvas.

        The run this window holds is shown as it is; any other is read back
        first, the way Open run does it - which waits, like Open run, for the
        run going here to finish or be parked.
        """
        if run_id and (self.run_state.cycle or {}).get("run_id") == run_id:
            self.come_forward()
            self.show_page("cycles")
            self.cycles.show_step(step_id)
            return
        if self.process.is_running():
            widgets.note(self._asked_from(), "Subjects",
                         "A run is going in this window. Another run can be opened once "
                         "it has finished, or after you park or stop it.")
            return
        if not run_dir:
            return
        self.come_forward()
        self.show_page("cycles")
        self._open_cycle_run(cycle_id, run_dir, then=lambda: self.cycles.show_step(step_id))

    def _questions_changed(self):
        """A run's question went up or came down: both Cockpits list what stands."""
        waiting = self.ask_bridge.waiting()
        self.cycles.set_questions(waiting)
        self.subjects_page.set_questions(waiting)

    def _resume_subject_run(self, cycle_id, run_dir, run_id):
        """Continue a subject's run where it stopped, keeping its saved mode.

        Read back onto the canvas first, the way Resume on the Cycles page acts
        on the run it is showing: the Cockpit then continues that run's record
        rather than starting a blank one.
        """
        if not (cycle_id and run_dir and run_id):
            return
        if self.process.is_running():
            widgets.note(self._asked_from(), "Subjects",
                         "A run is going in this window. Park or stop it, then resume this one.")
            return
        self.come_forward()
        self.show_page("cycles")
        self.cycles.show_loading(True)

        def opened(restored):
            if not restored:
                self.cycles.show_loading(False)
                widgets.warn(self, "Resume", "This run could not be read back.", run_dir)
                return
            self.cycles.open(cycle_id)
            # No mode: a Resume left to itself keeps the one the run was made in.
            self.start_cycle_run(cycle_id, "resume", run_id)

        self._replay(run_dir, opened)

    def _run_on_subject(self, subject):
        """Start a cycle the person picks on one subject, so it joins its history.

        A cycle that names what it works on takes the key through the variable
        its subject pins - the way a planned task is started; one that names
        nothing is told what the run is about (``--cycle-subject``).
        """
        key = (subject or {}).get("key") or ""
        if not key:
            return
        asked_from = self._asked_from()
        if self.process.is_running():
            widgets.note(asked_from, "Subjects", "A run is going in this window. Park or stop "
                                                 "it, then start another one.")
            return
        rows = [row for row in self.inventory.cycles if not row.get("problems")]
        if not rows:
            widgets.note(asked_from, "Subjects", "There is no cycle to run.")
            return
        labels = []
        for row in rows:
            label = row.get("name") or row.get("id", "")
            if row.get("project"):
                label = "%s - %s" % (label, row["project"])
            labels.append(label)
        newest = ((subject.get("cycles") or [{}])[0] or {}).get("id")
        current = next((index for index, row in enumerate(rows)
                        if row.get("id") == newest), 0)
        picked = widgets.ask_item(asked_from, "Run a cycle on it",
                                  "Which cycle should work on %s?" % key, labels,
                                  current=current,
                                  detail="What it asks and answers joins the history of %s."
                                         % key, agree="Run")
        if picked is None:
            return
        row = rows[labels.index(picked)]
        pin = (row.get("subject") or {}).get("pin") or ""
        if not pin:
            pin = next((one.get("pin") for one in row.get("triggers") or [] if one.get("pin")), "")
        self.come_forward()
        self.start_cycle_run(row["id"], variables={pin: key} if pin else {},
                             subject={"key": key, "title": subject.get("title") or "",
                                      "pin": pin})

    def park_cycle_run(self, run_id=""):
        """Put the live cycle run aside, to be resumed later, losing nothing.

        Pause first, so nothing new starts; then stop once only questions to a
        person are still going - a question is asked again on Resume - or once
        the run has paused. An agent in the middle of a call finishes that call
        first, because stopping it would throw away what it has been paid for.
        Stopping says "parked", which is how the run's subject knows it was put
        aside rather than abandoned.
        """
        cycle = self.run_state.cycle or {}
        if not self.run_state.cycle_running or not cycle.get("run_id"):
            return
        if run_id and cycle.get("run_id") != run_id:
            return
        lines = subjecthistory.park_lines(cycle)
        subject = cycle.get("subject") or {}
        named = subject.get("key") or cycle.get("name") or cycle.get("cycle") or "this run"
        if not widgets.confirm(self._asked_from(), "Park", "Park %s?" % named,
                               "The run stops and keeps everything it has done:\n- "
                               + "\n- ".join(lines), agree="Park"):
            return
        self._parking = cycle.get("run_id", "")
        if not subjecthistory.park_point(cycle):
            if cycle.get("control_version"):
                self.process.send_command(command="cycle.pause", run_id=self._parking)
            self.status_right.setText("parking - waiting for %s" % (
                ", ".join(subjecthistory.working(cycle)) or "the run to pause"))
        self._park_if_ready()

    def _park_if_ready(self):
        if not self._parking:
            return
        cycle = self.run_state.cycle or {}
        if not self.run_state.cycle_running or cycle.get("run_id") != self._parking:
            self._parking = ""
            return
        if not subjecthistory.park_point(cycle):
            return
        run_id, self._parking = self._parking, ""
        self._stopping = True
        cycle["_stopping"] = True
        self.status_right.setText("parked - resume it from Subjects")
        if not self.process.send_command(command="cycle.stop", run_id=run_id, reason="parked"):
            self.status_right.setText("The launcher is no longer accepting control commands.")

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
        try:
            where = cyclereplay.last_run_dir(self.history)
        except Exception:                 # noqa: BLE001 - cosmetic state only
            where = ""
        if not where:
            return

        def opened(restored):
            cycle_id = (self.run_state.cycle or {}).get("cycle", "") if restored else ""
            if cycle_id:
                self.cycles.open(cycle_id)

        self._replay(where, opened)

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

    # -- export and import ----------------------------------------------------
    def _users_rows(self, title):
        """users.json as it is now, or None with the reason said."""
        try:
            return usersfile.load(self.inventory.config_path or self.core.config_path)
        except usersfile.UsersFileError as exc:
            widgets.warn(self, title, "users.json cannot be read.", str(exc))
            return None

    def _cycle_projects(self):
        path = cpf.resolve_path(self.settings.cycle_projects_path)
        try:
            return path, cpf.load(path)
        except cpf.CycleProjectsFileError:
            return path, []

    def export_items(self, chosen=None):
        """File -> Export, or a page's Export with what it shows ticked."""
        rows = self._users_rows("Export")
        if rows is None:
            return
        self.launch_registry.reload()
        dialog = ExportDialog(self, self.launch_registry.projects,
                              self.launch_registry.environments(self.inventory), rows,
                              self.inventory.cycles, chosen,
                              secrets_included=True,
                              project_name=self._project(self.settings.project_id).name)
        if dialog.exec() != QDialog.Accepted:
            return
        from PySide6.QtWidgets import QFileDialog
        path, _filter = QFileDialog.getSaveFileName(
            self, "Export", os.path.join(widgets.start_dir("~"), dialog.suggested_name()),
            "QAVector export (*%s)" % transfer.SUFFIX)
        if not path:
            return
        if not path.endswith(".json"):
            path += transfer.SUFFIX
        chosen = dialog.selection()
        _path, cycle_projects = self._cycle_projects()
        try:
            document = transfer.bundle(
                dialog.launch_document_part(),
                transfer.cycles_part(self.inventory.cycles, chosen["cycles"],
                                     chosen["cycle_projects"], cycle_projects,
                                     secrets=self.core.cycle_secret_export))
            transfer.write(path, document)
        except (transfer.TransferError, core_mod.CoreError) as exc:
            widgets.warn(self, "Export", "Nothing was exported.", str(exc))
            return
        widgets.note(self, "Export", "Exported %s." % transfer.counts(document), path)

    def import_items(self, path=None):
        """File -> Import: an export file, or a single cycle file."""
        if not path:
            from PySide6.QtWidgets import QFileDialog
            path, _filter = QFileDialog.getOpenFileName(
                self, "Import", widgets.start_dir("~"),
                "QAVector export or cycle (*.json *.yaml *.yml)")
        if not path:
            return
        try:
            document = transfer.read(path)
        except transfer.TransferError as exc:
            widgets.warn(self, "Import", "This file cannot be imported.", str(exc))
            return
        rows = self._users_rows("Import")
        if rows is None:
            return
        self.launch_registry.reload()
        catalog = self.launch_registry.environments(self.inventory)
        projects_path, cycle_projects = self._cycle_projects()
        cycle_ids = [row.get("id") for row in self.inventory.cycles]
        items = transfer.plan(document, self.launch_registry.projects, catalog, rows,
                              [project.name for project in cycle_projects], cycle_ids)
        dialog = ImportDialog(self, path, document, items)
        if dialog.exec() != QDialog.Accepted:
            return
        mode = dialog.mode()
        said = []
        users_path = self.inventory.config_path or self.core.config_path
        if document["projects"] or document["environments"] or document["users"]:
            def change(projects):
                users = usersfile.load(users_path)
                said.extend(transfer.apply_launch(document, mode, projects, catalog,
                                                  users))
                problems = usersfile.validate(users) + lpf.validate(projects)
                if problems:
                    raise lpf.LaunchProjectsError("\n".join(problems))
                usersfile.save(users_path, users)
            try:
                self.launch_registry.update(change)
            except (lpf.LaunchProjectsError, usersfile.UsersFileError) as exc:
                widgets.warn(self, "Import", "Nothing was imported.", str(exc))
                return
        if document["cycles"] or document["cycle_projects"]:
            try:
                said.extend(transfer.apply_cycles(document, mode, self.core,
                                                  projects_path, cycle_ids))
            except (transfer.TransferError, core_mod.CoreError) as exc:
                said.append("Cycles were not imported: %s" % exc)
        widgets.output(self, "Import", "Imported from %s." % os.path.basename(path),
                       "\n".join(said))
        self._addresses_changed()
        self.environments.set_inventory_rows()
        self.refresh_inventory()

    def open_settings(self):
        dialog = SettingsDialog(self.settings, self, registry=self.action_registry)
        dialog.set_paths(*self.services.paths())
        dialog.set_flows_dir(self.inventory.dirs().get("flows") or "")
        dialog.set_run_dirs(self.inventory.dirs())
        if dialog.exec() == QDialog.Accepted:
            dialog.apply()
            self.launch_registry.path = lpf.resolve_path(self.settings.launch_projects_path)
            self.launch_registry.reload()
            self.core = self._new_core()
            self._aim_log_sources()
            self.listener.set_core(self.core)
            self.subjects_page.set_core(self.core)
            self.refresh_inventory()

    # -- projects -------------------------------------------------------------
    def _new_core(self):
        """A core for the project in use: every path from its settings."""
        return core_mod.Core(None, None, self.settings.config,
                             flows_dir=self.settings.flows_path,
                             cycles_dir=self.settings.cycles_path,
                             secrets_path=self.settings.cycle_secrets_path,
                             memory_path=self.settings.cycle_memory_path,
                             runs_path=self.settings.cycle_runs_path)

    def _adopt_launch_projects(self):
        """Once: the projects Launch Sessions had become projects of their own
        (an empty one) or part of Workspace (one with environments)."""
        if os.path.exists(projectsfile.default_path()):
            return
        registry = lpf.Registry(lpf.resolve_path(self.settings.launch_projects_path))
        if registry.error:
            return              # asked again next start, once the file reads
        try:
            if any(project.id != lpf.WORKSPACE for project in registry.projects):
                registry.update(projectsfile.adopt_launch_projects)
            else:
                projectsfile.adopt_launch_projects([])
        except (lpf.LaunchProjectsError, projectsfile.ProjectsFileError) as exc:
            logging.getLogger(__name__).warning("projects not adopted: %s", exc)

    def _projects(self):
        try:
            return projectsfile.load()
        except projectsfile.ProjectsFileError as exc:
            logging.getLogger(__name__).warning("%s", exc)
            return [projectsfile.workspace()]

    def _project(self, project_id):
        return projectsfile.find(self._projects(), project_id) or projectsfile.workspace()

    def _use_project(self, project):
        """Point the settings, and the files no setting names, at ``project``."""
        self.settings.use_project(project)
        if project.folder:
            projectsfile.make_folder(project.folder)
        clf.use_path(projectsfile.paths(project.folder).cycle_layout
                     if project.folder else None)

    #: The project list's last entry: not a project, the way to add one.
    NEW_PROJECT = "__new__"

    def _fill_projects(self):
        combo = self.project_combo
        combo.blockSignals(True)
        combo.clear()
        for project in self._projects():
            combo.addItem(project.name, project.id)
        combo.insertSeparator(combo.count())
        combo.addItem("New…", self.NEW_PROJECT)
        combo.setCurrentIndex(max(0, combo.findData(self.settings.project_id)))
        combo.blockSignals(False)

    def _project_picked(self, index):
        project_id = self.project_combo.itemData(index)
        if project_id == self.NEW_PROJECT:
            self._fill_projects()           # back to the one in use, whatever comes
            self.add_project()
            return
        if project_id and project_id != self.settings.project_id:
            if not self.switch_project(project_id):
                self._fill_projects()

    def _may_leave_project(self):
        """Whether the project in use can be left now; asks what has to be asked."""
        if self.process.is_running():
            widgets.note(self, "Projects", "A run is going.",
                         "Stop it, or let it finish, before picking another project: "
                         "it writes its results into this one.")
            return False
        unsaved = []
        if self.credentials.is_dirty():
            unsaved.append("users on Credentials")
        if self.scenarios.is_dirty():
            unsaved.append("the scenario being edited")
        if self.cycles.has_unsaved_draft():
            unsaved.append("the cycle being edited")
        if unsaved and not widgets.confirm(
                self, "Unsaved changes", "Drop the changes to %s?" % " and ".join(unsaved),
                "They are not saved, and another project has its own.",
                agree="Drop them", kind="warn"):
            return False
        if not self.services.confirm_discard():
            return False
        running = self.services.detained()
        if running and not widgets.confirm(
                self, "Services are running",
                "%d service(s) of this project stop when another one is picked."
                % len(running),
                ", ".join("%s · %s" % (one.project, one.name) for one in running[:6]),
                agree="Stop them and switch", kind="warn"):
            return False
        return True

    def switch_project(self, project_id, then=None):
        """Show ``project_id``: every page from its own files.

        ``then`` runs once its inventory has loaded - what Search does to show
        a row of another project. False when the project in use cannot be left.
        """
        if project_id == self.settings.project_id:
            if then is not None:
                then()
            return True
        if not self._may_leave_project():
            return False
        project = self._project(project_id)
        self.settings.current_project = project.id
        self._use_project(project)
        self.launch_registry.path = lpf.resolve_path(self.settings.launch_projects_path)
        self.launch_registry.reload()
        self.history.set_directory(self.settings.data_dir())
        self.switching.start("Opening %s…" % project.name)
        # Launch Sessions onto the new project's files FIRST: anything it saves
        # from here on - its context, a draft - must be that project's, never
        # the one left written under the new project's name.
        self.inventory = core_mod.Inventory()
        self.launch.project_name = project.name
        self.launch.switch_project(self.inventory)
        # Nothing of the project left stays on screen while this one is read:
        # every page that lists what the core read is emptied now.
        for page in (self.environments, self.command, self.launch, self.scenarios):
            page.set_inventory(self.inventory)
        self.cycles.set_inventory(self.inventory)
        self.core = self._new_core()
        self._aim_log_sources()
        self.listener.set_core(self.core)
        self.subjects_page.set_core(self.core)
        self.credentials.load(self.settings.config)
        self._fill_projects()
        self._after_switch = then
        self.refresh_inventory()
        return True

    def add_project(self):
        name = widgets.ask_text(self, "New project", "Name the project:", "",
                                "It starts empty, in a folder of its own: its users, "
                                "environments, services, scenarios and cycles.")
        if not name:
            return
        try:
            project = projectsfile.add(name)
        except projectsfile.ProjectsFileError as exc:
            widgets.warn(self, "New project", "The project was not added.", str(exc))
            return
        self._fill_projects()
        self.switch_project(project.id)

    def rename_project(self):
        project = self._project(self.settings.project_id)
        if project.id == projectsfile.WORKSPACE:
            widgets.note(self, "Rename project", "Workspace keeps its name.")
            return
        name = widgets.ask_text(self, "Rename project", "New name:", project.name)
        if not name or name == project.name:
            return
        try:
            projectsfile.rename(project.id, name)
        except projectsfile.ProjectsFileError as exc:
            widgets.warn(self, "Rename project", "The name was not changed.", str(exc))
            return
        self._fill_projects()
        self.launch.project_name = name.strip()
        self.launch.refresh_address()

    def delete_project(self):
        project = self._project(self.settings.project_id)
        if project.id == projectsfile.WORKSPACE:
            return
        if not widgets.confirm(
                self, "Delete project", "Take \"%s\" off the list?" % project.name,
                "Its folder stays on disk, with everything in it:\n%s" % project.folder,
                agree="Take it off", kind="warn"):
            return
        if not self.switch_project(projectsfile.WORKSPACE):
            return
        try:
            projectsfile.remove(project.id)
        except projectsfile.ProjectsFileError as exc:
            widgets.warn(self, "Delete project", "The project was not removed.", str(exc))
        self._fill_projects()

    def _project_action(self, action):
        """What the Environments page asked to be done to the project in use."""
        if action == "add":
            self.add_project()
        elif action == "rename":
            self.rename_project()
        elif action == "delete":
            self.delete_project()
        elif action == "export":
            self.export_project()

    def export_project(self):
        """File -> Export with the whole project ticked."""
        rows = self._users_rows("Export")
        if rows is None:
            return
        _path, cycle_projects = self._cycle_projects()
        names = {row.get("project") or "" for row in self.inventory.cycles}
        self.export_items({
            "projects": [project_id for project_id, _name in lpf.project_names(
                self.launch_registry.projects,
                self.launch_registry.environments(self.inventory))],
            "users": [person.key for person in usersfile.credentials(rows)
                      if person.login],
            "cycle_projects": sorted(names)})

    # -- new tasks ------------------------------------------------------------
    def _update_listening(self):
        line, detail, trouble = self.listener.status()
        self.listen_label.setText(line)
        self.listen_label.setToolTip(detail)
        self.listen_label.setStyleSheet("padding: 0 10px;%s" % (
            " color: %s;" % theme.BAD if trouble else ""))

    def _start_planned(self, item):
        """Start work on a task that was put on the plan - or, for an approval
        nobody answered, resume the run that stopped at it."""
        if self.process.is_running():
            return
        if item.get("kind") == "approval":
            try:
                self.core.cycle_plan_remove(item.get("id", ""))
            except core_mod.CoreError:
                pass
            self.cycles.refresh_planned()
            self.cycles.explorer.select(item["cycle"])
            self.start_cycle_run(item["cycle"], how="resume", step=item.get("run", ""))
            return
        pin = ""
        for row in self.inventory.cycles:
            if row.get("id") == item.get("cycle"):
                for one in row.get("triggers") or []:
                    if one.get("id") == item.get("trigger"):
                        pin = one.get("pin") or ""
        self.listener.seen(item)
        self.cycles.refresh_planned()
        self.cycles.explorer.select(item["cycle"])
        self.start_cycle_run(item["cycle"], variables={pin: item["key"]} if pin else {},
                             subject={"key": item["key"], "title": item.get("title", ""),
                                      "pin": pin})

    def _open_planned(self, item):
        """A planned entry double-clicked: start it now, or pick when."""
        from . import cycleschedule
        from .cyclesubjects import planned_title

        answer, run_at = cycleschedule.ask_when(self, item, title=planned_title(item))
        if answer == cycleschedule.START_NOW:
            self._start_planned(item)
            return
        if answer not in (cycleschedule.SCHEDULE, cycleschedule.CLEAR):
            return
        try:
            self.core.cycle_plan_schedule(item.get("id", ""), run_at)
        except core_mod.CoreError as exc:
            widgets.warn(self, "Start later", "The start time was not saved.", str(exc))
        self.cycles.refresh_planned()

    def _scheduled_due(self, item, missed):
        """A planned entry's time has come. Kept until nothing is running, and
        nobody is being asked anything else."""
        self._due.append((item, missed))
        self._start_due()

    def _start_due(self):
        if not self._due or self._offering or getattr(self, "_closing", False):
            return
        if self.process.is_running():
            return                              # _on_finished asks again
        from . import cycleschedule
        from .cyclesubjects import planned_title

        item, missed = self._due.pop(0)
        if missed:
            self._offering = True
            try:
                agreed = cycleschedule.ask_missed(self, item, planned_title(item))
            finally:
                self._offering = False
            if not agreed:
                try:
                    self.core.cycle_plan_schedule(item.get("id", ""), None)
                except core_mod.CoreError:
                    pass
                self.cycles.refresh_planned()
                QTimer.singleShot(0, self._start_due)
                return
        self.show_page("cycles")
        self._start_planned(item)

    def _ask_about(self, offer):
        """The "new task" window - see cyclewatch.ask_about."""
        from .cyclewatch import ask_about
        return ask_about(self, offer)

    def _offer_next(self):
        """Ask about the oldest new task - if nothing is running and nobody is
        already being asked. Start runs the cycle on it; Ignore never offers it
        again; Not now leaves it to be offered at the trigger's next check."""
        if self._offering or getattr(self, "_closing", False):
            return
        if self.process.is_running():
            return                              # _on_finished asks again
        offer = self.listener.next_offer()
        if offer is None:
            return
        self._offering = True
        try:
            answer = self._ask_about(offer)
        finally:
            self._offering = False
            self.listener.answered(offer)
            # A start that fell due while this was asked waited for it.
            QTimer.singleShot(0, self._start_due)
        if answer == "In plan":
            self.listener.plan(offer)
            self.cycles.refresh_planned()
        elif answer == "Start work":
            self.listener.seen(offer)
            self.show_page("cycles")
            self.cycles.explorer.select(offer["cycle"])
            self.start_cycle_run(offer["cycle"], variables=(
                {offer["pin"]: offer["key"]} if offer["pin"] else {}),
                subject={"key": offer["key"], "title": offer.get("title", ""),
                         "pin": offer["pin"]})
            return
        if answer == "Ignore":
            self.listener.seen(offer)
        QTimer.singleShot(0, self._offer_next)

    def one_shot(self, title, args):
        """Run a command that just prints something, and show what it printed."""
        if not self.core.is_configured():
            widgets.note(self, title, "The core was not found. Reinstall QAVector, or start "
                                                 "the GUI from a checkout.")
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
        if self.process.is_running():
            return
        login = self.ask_recording_account()
        if login == "cancel":
            return
        source, page = self._run_page(None)
        if source == "launch" and page.recording():
            # Launch Sessions' Record already says what it records into.
            scenario = page.record_target()
        else:
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
        chosen = widgets.ask_item(
            self, "Record", "Which account do you want to record?", logins,
            detail="Recording opens one window, and %d accounts are selected."
                   % len(logins))
        if not chosen:
            return "cancel"
        return chosen

    def _last_cycle(self):
        """``(id, name)`` of the cycle that ran last and is still here; ("", "")."""
        known = {row.get("id"): row for row in self.inventory.cycles}
        for entry in self.history.entries(history_mod.CYCLE):
            cycle_id = (entry.get("config") or {}).get("cycle") or ""
            if cycle_id in known:
                return cycle_id, known[cycle_id].get("name") or cycle_id
        return "", ""

    def _update_run_menu(self):
        """Run Last Cycle says which cycle, and is off when there is none or a
        run is going."""
        self._update_background_action()
        cycle_id, name = self._last_cycle()
        self.last_cycle_action.setText("Run &Last Cycle%s" % (" (%s)" % name if name else ""))
        self.last_cycle_action.setEnabled(bool(cycle_id) and not self.process.is_running())

    def run_last_cycle(self):
        cycle_id, _name = self._last_cycle()
        if not cycle_id or self.process.is_running():
            return
        self.show_page("cycles")
        self.cycles.explorer.select(cycle_id)
        self.start_cycle_run(cycle_id)

    def _update_background_action(self):
        """Offer In Background only for what can run with no browser, and say why not.

        Updated as the launch configuration changes and as the menu opens,
        so a configured hotkey has the same availability as the menu entry.
        Returns the blockers, empty when there are none.
        """
        _source, page = self._run_page(None)
        blockers = page.background_blockers()
        self.background_action.setEnabled(not blockers and self.run_action.isEnabled())
        self.background_action.setToolTip(_background_tip(blockers))
        shortcut = self.background_action.shortcut().toString(QKeySequence.NativeText)
        if shortcut:
            self.background_action.setToolTip(_background_tip(blockers) + "\n" + shortcut)
        return blockers

    def start_background_run(self):
        """RUN's menu -> In Background: the same launch, with no browser at all.

        Sessions, how many run at once and the load governor are exactly as the
        page says; only the windows are missing. Checked again here rather than
        trusted from when the menu opened, so a stale menu cannot start a run the
        launcher would refuse.
        """
        if self.process.is_running() or self._update_background_action():
            return
        self.start_run(background=True)

    def start_empty_run(self):
        """RUN's menu -> Empty Sessions: the same launch, every profile emptied first."""
        self.start_run(empty=True)

    def start_run(self, source=None, recorder=False, only_login=None, background=False,
                  empty=False):
        if self.process.is_running():
            return
        if not self.core.is_configured():
            widgets.note(self, "Run", "The core was not found. Reinstall QAVector, or start "
                                                 "the GUI from a checkout.")
            return
        source, page = self._run_page(source)
        if source == "launch" and not recorder and not background and not empty \
                and page.recording():
            # Record is one of Launch Sessions' answers to "what to do": the
            # same path as RUN -> With Recorder, which asks what it has to.
            self.start_recording()
            return
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
        if empty:
            args = commands.for_empty_sessions(args)
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
            *self._history_payload(source, page, args, argv, background, empty))
        if recorder:
            self.run.run_started(
                ("continuing %s · " % recorder if recorder is not True else "")
                + "recorder · press Capture Step (or F2) in a window")
        elif source == "launch":
            self.run.run_started("%s%s · events on stdout"
                                 % (EMPTY_PREFIX if empty else "",
                                    page.run_meta(background)))
        else:
            state = page.state()
            self.run.run_started("%s%sjobs=%s · %s · events on stdout"
                                 % (EMPTY_PREFIX if empty else "",
                                    "in the background · " if background else "",
                                    state.get("--jobs") or "1",
                                    state.get("--run-tests") or "(launch only)"))
        self.show_page("run")
        self.process.start(argv, working_dir=self.core.spawn_dir())

    def _history_payload(self, source, page, args, argv, background=False,
                         empty=False):
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
            return history_mod.LAUNCH, {
                "argv": list(argv), "display_command": display,
                "summary": (EMPTY_PREFIX if empty else "") + page.describe_line(background),
                "launch_config": page.state(),
                "command_state": page.command_state()}
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

    # -- search (Ctrl+K) --------------------------------------------------------
    def open_search(self):
        """Search, over whichever window of the application is in front."""
        if self._search is None:
            self._search = search.SearchPopup(
                self.search_hits,
                [(key, label) for key, label, _mark in CONFIGURE + OBSERVE], self,
                documents=self.search_documents)
            self._search.opened.connect(self.open_hit)
        self._search.open_over(QApplication.activeWindow() or self)

    def search_documents(self):
        """``(page key, folders)`` for every page whose files Search reads."""
        found = []
        for key, _label, _mark in CONFIGURE + OBSERVE:
            folders = getattr(self._pages.get(key), "search_documents", None)
            if folders is None or not self._offered(key):
                continue
            try:
                found.append((key, folders()))
            except Exception:           # noqa: BLE001 - one page must not end search
                logging.exception("Search could not read the %s page's folders", key)
        return found

    def search_hits(self):
        """What every page on the rail lists, filed under that page."""
        hits = []
        for key, _label, _mark in CONFIGURE + OBSERVE:
            page = self._pages.get(key)
            rows = getattr(page, "search_rows", None)
            if rows is None or not self._offered(key):
                continue
            try:
                found = rows()
            except Exception:           # noqa: BLE001 - one page must not end search
                logging.exception("Search could not read the %s page", key)
                continue
            hits.extend(one._replace(section=key) for one in found)
        # The other projects, from their own files: a row of any of them is
        # found, and opening it picks that project.
        offered = {key for key, _label, _mark in CONFIGURE + OBSERVE if self._offered(key)}
        try:
            hits.extend(one for one in projectsearch.other_projects(
                self.settings.project_id, self._workspace_paths())
                if one.section in offered)
        except Exception:               # noqa: BLE001 - one project must not end search
            logging.exception("Search could not read the other projects")
        return hits

    def _workspace_paths(self):
        """Workspace's files - its settings - for searching it from another project."""
        if not self.settings.project_folder:
            return None
        here = (self.settings.project_id, self.settings.project_folder)
        self.settings.use_project(projectsfile.workspace())
        try:
            from .store import app_data_dir
            return projectsfile.Paths(
                users=self.settings.config or self.core.config_path,
                launch_projects=lpf.resolve_path(self.settings.launch_projects_path),
                launch_presets=os.path.join(app_data_dir(), "launch-presets.json"),
                launch_drafts="", services=self.settings.services_path
                or os.path.join(core_mod.home_folder(), "services.json"),
                log_sources=self.settings.log_sources_path,
                flows=self.settings.flows_path, cycles=self.settings.cycles_path,
                cycle_projects="", cycle_layout="", cycle_secrets="", cycle_memory="",
                cycle_runs="", reports="", sessions="", history=app_data_dir())
        finally:
            self.settings.use_project(projectsfile.Project(here[0], "", here[1]))

    def come_forward(self):
        """This window in front, back from the taskbar if it was minimized.

        For a click in another window of the application that acts here - a
        search hit, a subject's section in a window of its own opening its run:
        what it opened must be on screen, not behind the window that asked.
        """
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def _asked_from(self):
        """Where a question about a click goes: over the window it was made in."""
        return QApplication.activeWindow() or self

    def open_hit(self, hit):
        """Go to where ``hit`` lives, with it on screen - from whatever window.

        A hit of another project picks that project first; the row is shown
        once its data has loaded.
        """
        self.come_forward()
        if hit.project and hit.project != self.settings.project_id:
            self.switch_project(hit.project, then=lambda: self.open_hit(
                hit._replace(project="")))
            return
        self.show_page(hit.section)
        page = self._pages.get(hit.section)
        if page is None or page.show_found(hit.target) is False:
            self.status_right.setText("%s is no longer there" % hit.title)

    def open_clarify(self):
        """The clarification popup, over whichever window is in front."""
        if self._clarify is None:
            self._clarify = cycleinteractions.ClarifyPopup(self)
            self._clarify.submitted.connect(self._send_cycle_interaction)
            self._clarify.sent.connect(self.status_right.setText)
            # Kept current while it is open, as the Stages composer is: who is
            # working changes under it, and so does what became of a message.
            self.run_state.changed.connect(self._refresh_clarify)
            self.run_state.cycle_interactions_changed.connect(self._refresh_clarify)
        self._refresh_clarify(force=True)
        self._clarify.open_over(QApplication.activeWindow() or self)

    def _refresh_clarify(self, force=False):
        if self._clarify is None or not (force or self._clarify.isVisible()):
            return
        self._clarify.set_context(self.run_state.cycle, self.run_state.cycle_running,
                                  self.cycles.step_labels(),
                                  selected=self.cycles.selected_step())

    def _send_cycle_interaction(self, request):
        cycle = self.run_state.cycle or {}
        if request.get("run_id") != cycle.get("run_id"):
            return
        self.run_state.interaction_sending(request)
        if not self.run_state.cycle_running or not self.process.send_command(**request):
            self.run_state.handle({"kind": "cycle.interaction.refused",
                                   "run_id": request.get("run_id"),
                                   "id": request.get("id"),
                                   "reason": "The launcher is no longer accepting input."})

    def _start_page_cycle_run(self, cycle_id, how="", step=""):
        mode = self.cycles.requested_execution_mode()
        # Only a choice the core would not make by itself is sent. Without the
        # flag a new run takes the cycle's own default, and a core older than
        # execution modes refuses the flag outright - so the ordinary Run keeps
        # working against it. A resume always says which mode it wants.
        if how != "resume" and mode == self.cycles.default_execution_mode():
            mode = None
        self.start_cycle_run(cycle_id, how, step, execution_mode=mode)

    def start_cycle_run(self, cycle_id, how="", step="", variables=None, subject=None, execution_mode=None):
        """Run one cycle. Modelled on start_run, and deliberately separate.

        A scenario run is assembled from a page's form; a cycle run is one id
        and the flags the core needs to report it. Folding the two together
        would mean a page-shaped branch inside start_run for something that
        shares only its guards.

        It stays on the Cycles page, where Run Cockpit opens from the start
        event. The Run Graph and raw output remain available there.

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
            widgets.note(self, "Run", "The core was not found. Reinstall QAVector, or start "
                                                 "the GUI from a checkout.")
            return
        args = (["--cycle-run=" + cycle_id, "--events=-", "--control=-"]
                + self.core.secrets_flag() + self.core.memory_flag()
                + ["--cycle-var=%s=%s" % one for one in sorted((variables or {}).items())])
        # A project's browser profiles are its own; its scenario steps are
        # handed them along with --config and --log-sources.
        if self.settings.project_folder:
            args.append("--sessions-dir=" + self.settings.directory("sessions"))
        if execution_mode in ("auto", "important", "approve"):
            args.append("--cycle-mode=" + execution_mode)
        if subject and subject.get("key") and not how:
            # Started from an offer or the plan: the run is about what was
            # offered, so its row keeps the name the plan gave it.
            args.append("--cycle-subject=" + subject["key"])
            if subject.get("title"):
                args.append("--cycle-subject-title=" + subject["title"])
            if subject.get("pin"):
                args.append("--cycle-subject-pin=" + subject["pin"])
        if how == "resume" and step:
            args.append("--cycle-resume=" + step)
        elif how == "action" and step:
            args.append("--cycle-action=" + step)
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
            "config": {"cycle": cycle_id, **({"action": step} if how == "action" else {})},
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
        if self.run_state.cycle_running and self.run_state.cycle:
            self.run_state.cycle["_stopping"] = True
            self.run_state.changed.emit()
        self.status_right.setText("stopping — the launcher closes its windows first")
        self.process.stop()

    def _show_cycle_cockpit(self):
        self.show_page("cycles")
        self.cycles.show_cockpit()

    def _cycle_navigation_needed(self):
        cycle = self.run_state.cycle or {}
        return not (getattr(self, "_page_key", "") == "cycles" and self.cycles.current_id() == cycle.get("cycle"))

    def _confirm_cycle_supervision(self):
        cycle = self.run_state.cycle or {}
        if self.run_state.cycle_running and cycle.get("timing_version") and self.process.is_running():
            self.process.send_command(command="cycle.supervision", run_id=cycle.get("run_id"))

    def _control_cycle(self, action, phase=""):
        cycle = self.run_state.cycle or {}
        if action == "set_mode":
            if (self.run_state.cycle_running and cycle.get("paused") and cycle.get("execution_policy_version")
                    and phase in ("auto", "important", "approve")):
                if not self.process.send_command(command="cycle.mode.change", run_id=cycle.get("run_id"), mode=phase):
                    self.status_right.setText("The launcher is no longer accepting control commands.")
            return
        commands = {"pause": "cycle.pause", "continue": "cycle.continue", "run_phase": "cycle.phase.run", "retry_phase": "cycle.phase.retry"}
        if not self.run_state.cycle_running or not cycle.get("control_version") or action not in commands:
            return
        if not self.process.send_command(command=commands[action], run_id=cycle.get("run_id"), **({"phase": phase} if phase else {})):
            self.status_right.setText("The launcher is no longer accepting control commands.")

    def _answer_cycle_node_approval(self, request_id, fingerprint, approved):
        cycle = self.run_state.cycle or {}
        receipt = (cycle.get("node_approvals") or {}).get(request_id) or {}
        if (not self.run_state.cycle_running or not cycle.get("execution_policy_version")
                or receipt.get("status") != "pending" or receipt.get("fingerprint") != fingerprint):
            return
        if not self.process.send_command(command="cycle.node.approval.answer", run_id=cycle.get("run_id"),
                                         request_id=request_id, fingerprint=fingerprint, approved=bool(approved)):
            self.status_right.setText("The launcher is no longer accepting approval answers.")

    def _on_started(self, argv):
        self._set_running(True)
        self.log.append({"ts": "", "level": "INFO", "session": "",
                         "text": "$ " + " ".join(argv)})

    def _on_event(self, event):
        self.run_state.handle(event)
        kind = event.get("kind")
        if (kind == "flow.end" and self._entry_id is not None and not self._stopping
                and event.get("status") in ("pass", "fail", "error")):
            self.history.note(self._entry_id, last_scenario_status=event["status"])
        if kind == "cycle.control.refused":
            self.status_right.setText(event.get("reason", "Control action refused."))
        if kind == "cycle.run.start":
            self._confirm_cycle_supervision()
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
        # A task that turned up during the run was kept waiting for this.
        QTimer.singleShot(0, self._offer_next)
        # And so was a start somebody scheduled for while it ran.
        QTimer.singleShot(0, self._start_due)
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
        self.record_action.setEnabled(not running)
        self.empty_action.setEnabled(not running)
        self._update_background_action()
        self.command.set_running(running)
        self.launch.set_running(running)
        self.status_state.setText("state: running" if running else "state: idle")
        self._update_workers()   # the count is a live fact; it goes when the run does
        self._subjects_follow()
        if not running:
            self._parking = ""
        if running:
            self.status_right.setText("events: stdout · logs: stderr")

    # -- history --------------------------------------------------------------
    def restore_entry(self, entry):
        """Put a recorded run back into the page that produced it."""
        kind = entry.get("kind")
        if kind == history_mod.LAUNCH:
            # As (unsaved): never as an edit of whichever configuration is open,
            # which Save would then have written over.
            self.launch.open_unsaved(entry.get("launch_config") or {})
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
        # Running in the background, or on emptied profiles, is part of what was
        # asked for, even though no saved configuration keeps it - the command it
        # became does.
        argv = entry.get("argv") or []
        self.start_run(source, background="--no-browser" in argv,
                       empty="--empty-sessions" in argv)

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

    def _open_artifacts(self, run_dir, path=""):
        """A run's folder on the Artifacts page, with ``path`` in it selected.

        ``path`` is relative to the run, the way a cycle records its files.
        With the Artifacts page switched off in View there is nowhere in the
        window to show it, so the desktop is handed the folder instead.
        """
        if not run_dir:
            return
        if not self._offered("artifacts"):
            where = os.path.join(run_dir, path) if path else run_dir
            if not os.path.isdir(where):
                where = os.path.dirname(where)
            QDesktopServices.openUrl(QUrl.fromLocalFile(where))
            return
        self.artifacts.reveal(run_dir, path)
        self.show_page("artifacts")

    # -- Stages, wherever they are ----------------------------------------------
    def set_stages_detached(self, detached):
        """Stages in a window of its own, or back in its tab. The View switch."""
        if detached:
            self.cycles.detach_stages()
        else:
            self.cycles.dock_stages()

    def _stages_detached_changed(self, detached):
        self.stages_window_action.blockSignals(True)
        self.stages_window_action.setChecked(bool(detached))
        self.stages_window_action.blockSignals(False)

    def go_to_stages(self):
        """Ctrl+Shift+S: Stages in front, whether a tab or a window of its own."""
        if self.cycles.is_stages_detached():
            self.cycles.show_stages()
            return
        if not self._offered("cycles"):
            self.status_right.setText("Cycles is switched off in View > Sidebar items")
            return
        self._to_front(self)
        self.show_page("cycles")
        self.cycles.show_stages()

    @staticmethod
    def _to_front(window):
        """Back from the taskbar if minimized, and in front. Keeps maximized."""
        state = window.windowState()
        if state & Qt.WindowMinimized:
            window.setWindowState(state & ~Qt.WindowMinimized)
        if not window.isVisible():
            return
        window.raise_()
        window.activateWindow()

    def showEvent(self, event):              # noqa: N802 - Qt's own spelling
        """Stages goes back into its own window if it was left in one.

        On the first show rather than in the constructor, so that neither the
        splash nor a headless --launch - which never shows this window - opens
        a window of Stages before the application is on screen, or at all.
        """
        super().showEvent(event)
        if not self._stages_restored:
            self._stages_restored = True
            if self.settings.stages_detached:
                QTimer.singleShot(0, self._restore_stages_window)

    def _restore_stages_window(self):
        if getattr(self, "_closing", False) or self.cycles.is_stages_detached():
            return
        self.cycles.detach_stages()
        # Opened beside the window, not over it: the application was started,
        # and it is the main window somebody expects in front.
        self.raise_()
        self.activateWindow()

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
        AboutDialog(self, description=self.about_text()).exec()

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
        read_from, _target = lsf.resolve_path(
            self.settings.log_sources_path,
            "" if self.settings.project_folder else self.core.legacy_log_sources)
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
            services.set_badges([(up, "ok"), (down, "bad")],
                                _running_and_failed(up, down))
        cycles = self._nav_buttons.get("cycles")
        if cycles is not None:
            # The same two points as Services, for the same two questions: is
            # one going, and did the last one go wrong.
            up, down = self.run_state.cycle_counts()
            cycles.set_badges([(up, "ok"), (down, "bad")],
                              _running_and_failed(up, down))
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
        label = self._nav_labels.get(key, "") if self.settings.sidebar_collapsed else ""
        action = getattr(self, "_page_actions", {}).get(key)
        shortcut = action.shortcut().toString(QKeySequence.NativeText) if action else ""
        return "\n".join(filter(None, (label, counts, shortcut)))

    def _refresh_hotkey_hints(self):
        """Dynamic hints keep their live counts as shortcuts are changed."""
        for key, button in self._nav_buttons.items():
            button.setToolTip(self._nav_tooltip(key))
        hint = ("Expand the sidebar" if self.settings.sidebar_collapsed
                else "Collapse the sidebar")
        shortcut = self.collapse_action.shortcut().toString(QKeySequence.NativeText)
        self.collapse_button.setToolTip("%s (%s)" % (hint, shortcut) if shortcut else hint)
        self.cycles.output.set_stages_shortcut(self.go_stages_action.shortcut())
        self._update_background_action()

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
        # Back in its tab first: a row of Subjects going down with the window
        # must not take the Cycles page's Stages panel with it - and the
        # subjects open in windows of their own go with this one.
        self.subjects_page.shut_windows()
        # Stages lives only while this window does. Shut, not docked: it was
        # detached when the application went, so it is detached when it comes
        # back.
        self.cycles.shut_stages_window()
        self.listener.shutdown()
        self._listen_timer.stop()
        self._metrics_timer.stop()
        self.settings.save_geometry(self.saveGeometry())
        # A QThread whose QObject is destroyed while it is still running takes the
        # process down with it, which turns closing the window mid-describe into a
        # crash on the way out. --describe is a subprocess we cannot cancel, so
        # wait for it rather than pretend it can be stopped.
        if self.loader_thread is not None and self.loader_thread.isRunning():
            self.loader_thread.wait(5000)
        # The same for the pages' own calls still out - see background.
        background.wait_all(5000)
        super().closeEvent(event)


def _pyside_version():
    try:
        import PySide6
        return PySide6.__version__
    except Exception:
        return "?"
