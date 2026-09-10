"""Launch Sessions: configure a run in the words of the job, not the CLI.

The Command page next door is a control per flag. This page is the same launcher
described the other way round - which environment, whose accounts, what to run,
how much to record - and :mod:`cms_gui.launch` does the translating. Nothing here
builds a command line; it builds a configuration and asks that module for the
argv, which is why a selection made here and the same selection made on the
Command page cannot mean two different things.

The page never shows a path the user has to type, a flag name, or a comma-
separated list to assemble by hand. The one exception is Advanced, folded away,
where a power user who does know what a reports directory is can set one.
Where the scenarios live is not among them: that is one answer for the whole
application, so it is Settings -> Scenarios rather than a per-run override.
"""

import json
import os

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog,
                               QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QMenu, QMessageBox, QPushButton, QRadioButton,
                               QScrollArea, QToolButton, QVBoxLayout, QWidget)

from .. import icons, launch, load as load_mod, store, theme, widgets

SCENARIO_MODES = [
    (launch.SCENARIOS_NONE, "Just open the windows",
     "Sign every account in and leave the windows there."),
    (launch.SCENARIOS_ALL, "Run every scenario",
     "Everything the flows folder offers, template and manual ones excluded."),
    (launch.SCENARIOS_PER_USER, "Use each account's own list",
     "Each account runs the scenarios set for it on the Credentials page."),
    (launch.SCENARIOS_PICK, "Choose scenarios",
     "Pick them below. A tag pulls in every scenario carrying it."),
]

UNSAVED = "(unsaved)"
ALL_ENVIRONMENTS = "All environments"

class DesktopLinkDialog(QDialog):
    def __init__(self, parent, default_name, default_path, default_icon):
        super().__init__(parent)
        self.setWindowTitle("Configure Desktop Link")
        self.resize(int(parent.width() * 0.5), self.height())
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

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.inventory = None
        self.core = None
        # True until _restore() has put the saved state back. Building the
        # controls fires their signals - a checkbox set to its default, a fold
        # restored from settings - and letting those reach _changed() would save
        # the empty default page over the state that is about to be restored.
        self._building = True
        # The saved configuration as it was when it was opened, so an edit made
        # since can be told from the file on disk. None while nothing is opened.
        self._baseline = None
        self._dirty = False
        self._developer = bool(settings.developer_mode)
        self._configs = store.NamedConfigs(
            os.path.join(store.app_data_dir(), "configs.json"))

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
        column.addWidget(self._config_bar())
        column.addSpacing(14)

        grid = QGridLayout()
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(20)
        grid.addWidget(self._environment_panel(), 0, 0)
        grid.addWidget(self._users_panel(), 1, 0)
        grid.addWidget(self._extensions_panel(), 2, 0)
        grid.addWidget(self._sessions_panel(), 0, 1)
        grid.addWidget(self._scenarios_panel(), 1, 1)
        grid.addWidget(self._reports_panel(), 2, 1)
        grid.addWidget(self._server_logs_panel(), 3, 0, 1, 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(1, 1)
        column.addLayout(grid)
        column.addSpacing(16)
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
    def _config_bar(self):
        panel = widgets.BlueprintPanel(padding=(14, 12, 14, 12))
        self.config_combo = QComboBox()
        self.config_combo.setMinimumWidth(240)
        self.config_combo.currentTextChanged.connect(self._config_selected)
        self.config_save = save = QPushButton("Save")
        save.clicked.connect(self.save_configuration)
        duplicate = QPushButton("Duplicate")
        duplicate.clicked.connect(self.duplicate_configuration)
        rename = QPushButton("Rename")
        rename.clicked.connect(self.rename_configuration)
        delete = QPushButton("Delete")
        delete.clicked.connect(self.delete_configuration)
        panel.layout().addWidget(widgets.row(
            widgets.kicker("saved configuration"), self.config_combo, None,
            save, duplicate, rename, delete))
        self._reload_configs()
        return panel

    def _environment_panel(self):
        panel = widgets.BlueprintPanel()
        panel.layout().addWidget(widgets.kicker("Environment"))
        self.env_combo = QComboBox()
        self.env_combo.addItem(ALL_ENVIRONMENTS)
        self.env_combo.currentTextChanged.connect(self._environment_changed)
        panel.layout().addWidget(widgets.field(
            "Which system to sign in to", self.env_combo,
            "Each environment supplies its own address, so no URL is needed."))
        self.env_note = widgets.mono("")
        panel.layout().addWidget(self.env_note)
        panel.layout().addStretch(1)
        return panel

    def _users_panel(self):
        panel = widgets.BlueprintPanel()
        self.users_mode = widgets.Segmented(["All accounts", "Choose accounts"])
        self.users_mode.changed.connect(lambda _v: self._users_mode_changed())
        panel.layout().addWidget(widgets.row(widgets.kicker("Users"), None,
                                             self.users_mode))
        self.users_list = widgets.CheckList(placeholder="Search accounts…")
        self.users_list.set_noun("accounts")
        self.users_list.changed.connect(self._changed)
        panel.layout().addWidget(self.users_list, 1)
        all_button = QPushButton("Select all")
        all_button.setProperty("variant", "ghost")
        all_button.clicked.connect(lambda: self.users_list.set_all(True))
        none_button = QPushButton("Select none")
        none_button.setProperty("variant", "ghost")
        none_button.clicked.connect(lambda: self.users_list.set_all(False))
        self.users_hint = QLabel("Accounts come from the Credentials page.")
        self.users_hint.setProperty("role", "hint")
        panel.layout().addWidget(widgets.row(all_button, none_button, None,
                                             self.users_hint))
        return panel

    def _extensions_panel(self):
        panel = widgets.BlueprintPanel()
        self.ext_mode = widgets.Segmented(["All", "None", "Choose"])
        self.ext_mode.changed.connect(lambda _v: self._ext_mode_changed())
        panel.layout().addWidget(widgets.row(widgets.kicker("Extensions"), None,
                                             self.ext_mode))
        panel.layout().addWidget(widgets.lede(
            "Browser add-ons installed into every profile before it opens."))
        self.ext_list = widgets.CheckList(searchable=False)
        self.ext_list.set_noun("extensions")
        self.ext_list.list.setMinimumHeight(96)
        self.ext_list.changed.connect(self._changed)
        panel.layout().addWidget(self.ext_list, 1)
        return panel

    def _server_logs_panel(self):
        panel = widgets.BlueprintPanel()
        self.logs_mode = widgets.Segmented(["Off", "Default", "All", "Choose"])
        self.logs_mode.changed.connect(lambda _v: self._logs_mode_changed())
        panel.layout().addWidget(widgets.row(widgets.kicker("Server logs"), None,
                                             self.logs_mode))
        panel.layout().addWidget(widgets.lede(
            "The backend's own log, streamed into each session's panel and shown "
            "only the lines written after that window opened."))
        self.logs_list = widgets.CheckList(searchable=False)
        self.logs_list.set_noun("logs")
        self.logs_list.list.setMinimumHeight(96)
        self.logs_list.changed.connect(self._changed)
        panel.layout().addWidget(self.logs_list, 1)
        self.logs_note = QLabel("")
        self.logs_note.setProperty("role", "hint")
        self.logs_note.setWordWrap(True)
        panel.layout().addWidget(self.logs_note)
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
        alias = self.env_combo.currentText()
        alias = "" if alias == ALL_ENVIRONMENTS else alias
        rows = []
        if self.inventory:
            env_value = self.inventory.env_value(alias) if alias else None
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

    def _sessions_panel(self):
        panel = widgets.BlueprintPanel()
        panel.layout().addWidget(widgets.kicker("Sessions"))
        self.jobs = widgets.Stepper(1, 64)
        self.jobs.valueChanged.connect(self._changed)
        self.jobs_all = QCheckBox("All at once")
        self.jobs_all.toggled.connect(self._jobs_all_toggled)
        self.jobs_auto = QCheckBox("Auto")
        self.jobs_auto.setToolTip(
            "Start at one window per processor core and let the machine decide: "
            "fewer while memory is under real pressure, back up again once it is "
            "not. A number you type here is never changed for you.")
        self.jobs_auto.toggled.connect(self._jobs_auto_toggled)
        panel.layout().addWidget(widgets.field(
            "How many sessions run at the same time",
            widgets.row(self.jobs, self.jobs_all, self.jobs_auto, None),
            "One at a time is the calmest to watch; more is faster. "
            "\"Auto\" lets the machine raise and lower it as the run goes."))
        self.keep_open = QCheckBox("Leave the windows open when the run finishes")
        self.keep_open.setChecked(True)
        self.keep_open.toggled.connect(self._changed)
        panel.layout().addWidget(self.keep_open)
        
        self.metrics_warning = QLabel("System Load: CPU --% | RAM --%")
        self.metrics_warning.setProperty("role", "hint")
        self.metrics_warning.setWordWrap(True)
        panel.layout().addWidget(self.metrics_warning)

        panel.layout().addStretch(1)
        return panel

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

    def _scenarios_panel(self):
        panel = widgets.BlueprintPanel()
        panel.layout().addWidget(widgets.kicker("Scenarios"))
        self.scenario_mode = QButtonGroup(self)
        self.scenario_mode.setExclusive(True)
        for index, (mode, label, hint) in enumerate(SCENARIO_MODES):
            button = QRadioButton(label)
            button.setToolTip(hint)
            self.scenario_mode.addButton(button, index)
            panel.layout().addWidget(button)
        self.scenario_mode.button(0).setChecked(True)
        self.scenario_mode.idToggled.connect(self._scenario_mode_changed)

        self.scenario_list = widgets.CheckList(placeholder="Search scenarios and tags…")
        self.scenario_list.set_noun("scenarios")
        self.scenario_list.changed.connect(self._changed)
        panel.layout().addWidget(self.scenario_list, 1)
        return panel

    def _reports_panel(self):
        panel = widgets.BlueprintPanel()
        panel.layout().addWidget(widgets.kicker("Reports and screenshots"))
        self.report_mode = widgets.Segmented(["Results only", "Full diagnostics",
                                             "Choose"])
        self.report_mode.changed.connect(lambda _v: self._report_mode_changed())
        panel.layout().addWidget(self.report_mode)
        self.report_list = widgets.CheckList(searchable=False)
        self.report_list.set_noun("artifacts")
        self.report_list.list.setMinimumHeight(90)
        self.report_list.changed.connect(self._changed)
        panel.layout().addWidget(self.report_list)

        self.shots = QComboBox()
        for mode in (launch.SHOTS_OFF, launch.SHOTS_FINISH,
                     launch.SHOTS_START_FINISH, launch.SHOTS_EACH):
            self.shots.addItem(launch.SHOT_LABELS[mode], mode)
        self.shots.currentIndexChanged.connect(self._changed)
        panel.layout().addWidget(widgets.field("Screenshots", self.shots))
        self.report_always = QCheckBox("Keep the full report even when everything "
                                       "passes")
        self.report_always.toggled.connect(self._changed)
        panel.layout().addWidget(self.report_always)
        panel.layout().addStretch(1)
        return panel

    def _advanced_panel(self):
        panel = widgets.BlueprintPanel()
        self.advanced = widgets.Disclosure("Advanced")
        self.advanced.toggled.connect(self._on_advanced_toggled)
        panel.layout().addWidget(self.advanced)
        inner = self.advanced.body()
        inner.addWidget(widgets.lede(
            "Overrides most runs never need. Left empty, each of these follows "
            "the launcher's own default."))

        top = QHBoxLayout()
        top.setSpacing(16)
        self.url = QLineEdit()
        self.url.setPlaceholderText("supplied by the environment")
        self.url.textChanged.connect(self._changed)
        top.addWidget(widgets.field("Start address", self.url), 2)
        self.profile_prefix = QLineEdit()
        self.profile_prefix.setPlaceholderText("named after the environment")
        self.profile_prefix.textChanged.connect(self._changed)
        top.addWidget(widgets.field("Profile folder prefix", self.profile_prefix), 1)
        self.log_level = QComboBox()
        for level in launch.LOG_LEVELS:
            self.log_level.addItem(level)
        self.log_level.setCurrentText("INFO")
        self.log_level.currentTextChanged.connect(self._changed)
        top.addWidget(widgets.field("Log detail", self.log_level), 1)
        inner.addLayout(top)

        self.dir_edits = {}
        dirs = QHBoxLayout()
        dirs.setSpacing(16)
        for key, label in (("reports_dir", "Reports folder"),
                           ("sessions_dir", "Profiles folder")):
            edit = QLineEdit()
            edit.setProperty("mono", True)
            edit.setPlaceholderText("(default)")
            edit.textChanged.connect(self._changed)
            browse = icons.button(QPushButton(""), "browse")
            browse.setFixedWidth(38)
            browse.clicked.connect(lambda _c=False, e=edit: self._browse(e))
            self.dir_edits[key] = edit
            dirs.addWidget(widgets.field(label, widgets.row(edit, browse)), 1)
        inner.addLayout(dirs)

        self.overlay = QCheckBox("Show an execution overlay inside each window")
        self.overlay.toggled.connect(self._overlay_toggled)
        inner.addWidget(self.overlay)
        self.overlay_list = widgets.CheckList(searchable=False)
        self.overlay_list.set_noun("overlay parts")
        self.overlay_list.list.setMaximumHeight(120)
        self.overlay_list.changed.connect(self._changed)
        inner.addWidget(self.overlay_list)
        self.detach = QCheckBox("Leave the windows running after the launcher exits")
        self.detach.toggled.connect(self._changed)
        inner.addWidget(self.detach)
        return panel

    def _footer(self):
        footer = QFrame()
        footer.setProperty("role", "footer")
        column = QVBoxLayout(footer)
        column.setContentsMargins(24, 10, 24, 10)
        column.setSpacing(6)

        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.summary.setProperty("role", "summary")
        reset = QPushButton("Reset")
        reset.clicked.connect(self.reset)
        self.footer_save = save = QPushButton("Save")
        save.clicked.connect(self.save_configuration)
        # Everything here is something you do to a configuration now and then -
        # never mid-flow - so it folds into one unlabelled button rather than
        # standing beside Save and RUN competing for the same glance.
        self.more_button = QToolButton()
        icons.button(self.more_button, "more", "")
        self.more_button.setPopupMode(QToolButton.InstantPopup)
        self.more_button.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.more_button.setProperty("menuglyph", "true")
        self.more_button.setToolTip("More options for this configuration")
        self.more_menu = QMenu(self.more_button)
        self.desktop_link = QAction("Desktop Link", self)
        self.desktop_link.setCheckable(True)
        self.desktop_link.setToolTip(
            "Put a launcher for this configuration on the desktop.")
        self.desktop_link.triggered.connect(self._toggle_desktop_link)
        self.more_menu.addAction(self.desktop_link)
        self.more_button.setMenu(self.more_menu)
        # theme.BG, not the default ink: a primary button paints its label in
        # the background colour against the accent fill, and the mark beside it
        # has to be the same colour or it reads as a different control.
        self.run_button = icons.button(QPushButton("RUN"), "run", color=theme.BG)
        self.run_button.setProperty("variant", "primary")
        self.run_button.clicked.connect(self.run_requested.emit)
        # Built by hand rather than with widgets.row: the summary is the long part
        # and has to take the leftover width, which needs a stretch factor on that
        # one widget instead of a spacer beside it.
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
        strip.addWidget(self.summary, 1)
        for button in (reset, save, self.more_button, self.run_button):
            strip.addWidget(button)
        column.addLayout(strip)

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
        alias = self.env_combo.currentText()
        return {
            "environment": "" if alias == ALL_ENVIRONMENTS else alias,
            "users": {"mode": (launch.USERS_PICK
                               if self.users_mode.current() == "Choose accounts"
                               else launch.USERS_ALL),
                      "logins": self.users_list.checked()},
            "sessions": {"jobs": self.jobs.value(),
                         "all_at_once": self.jobs_all.isChecked(),
                         "auto_jobs": self.jobs_auto.isChecked(),
                         "keep_open": self.keep_open.isChecked(),
                         "detach": self.detach.isChecked()},
            "extensions": {"mode": self._ext_mode_value(),
                           "names": self.ext_list.checked()},
            "server_logs": {"mode": self._logs_mode_value(),
                            "names": self.logs_list.checked()},
            "scenarios": {"mode": SCENARIO_MODES[self._scenario_mode_id()][0],
                          "selected": self.scenario_list.checked()},
            "reports": {"level": self._report_mode_value(),
                        "artifacts": self.report_list.checked(),
                        "always": self.report_always.isChecked()},
            "screenshots": {"mode": self.shots.currentData() or launch.SHOTS_OFF},
            "overlay": {"enabled": self.overlay.isChecked(),
                        "components": self.overlay_list.checked()},
            "advanced": {"url": self.url.text().strip(),
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
            self._select_env(config["environment"])

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

            extensions = config["extensions"]
            self.ext_mode.set_current({launch.EXT_NONE: "None",
                                       launch.EXT_PICK: "Choose"}.get(
                                          extensions["mode"], "All"), notify=False)
            self._populate_extensions(extensions["names"])

            server_logs = config["server_logs"]
            self.logs_mode.set_current({launch.LOGS_DEFAULT: "Default",
                                        launch.LOGS_ALL: "All",
                                        launch.LOGS_PICK: "Choose"}.get(
                                            server_logs["mode"], "Off"), notify=False)
            self._populate_server_logs(server_logs["names"])

            scenarios = config["scenarios"]
            for index, (mode, _label, _hint) in enumerate(SCENARIO_MODES):
                if mode == scenarios["mode"]:
                    self.scenario_mode.button(index).setChecked(True)
                    break
            self._populate_scenarios(scenarios["selected"])

            reports = config["reports"]
            self.report_mode.set_current({launch.REPORTS_FULL: "Full diagnostics",
                                          launch.REPORTS_CUSTOM: "Choose"}.get(
                                             reports["level"], "Results only"),
                                         notify=False)
            self._populate_artifacts(reports["artifacts"])
            self.report_always.setChecked(bool(reports["always"]))
            index = self.shots.findData(config["screenshots"]["mode"])
            self.shots.setCurrentIndex(index if index >= 0 else 0)

            self.overlay.setChecked(bool(config["overlay"]["enabled"]))
            self._populate_overlay(config["overlay"]["components"])

            advanced = config["advanced"]
            self.url.setText(advanced["url"])
            self.profile_prefix.setText(advanced["profile_prefix"])
            self.log_level.setCurrentText(advanced["log_level"] or "INFO")
            for key, edit in self.dir_edits.items():
                edit.setText(advanced[key])
        finally:
            self._building = False
        self._changed()

    def argv(self):
        return launch.argv(self.state(), self.inventory)

    def problem_list(self):
        return launch.validate(self.state(), self.inventory)

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
        return launch.summarise(self.state(), self.inventory)

    # -- reactions ------------------------------------------------------------
    def _changed(self, *_args):
        if self._building:
            return
        config = self.state()
        self.users_list.setEnabled(config["users"]["mode"] == launch.USERS_PICK)
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

        rows = launch.summarise(config, self.inventory)
        self.summary.setText("   ·   ".join("%s: %s" % (label, value)
                                            for label, value in rows))
        problems = launch.validate(config, self.inventory)
        self.problems.setText("   ".join(problems))
        # Never gated by the fold: a problem is the reason RUN would refuse, and
        # a refusal with its explanation folded away is the worst of both.
        self.problems.setVisible(bool(problems))
        notes = launch.notes(config, self.inventory)
        self.notes.setText("   ".join(notes))
        expanded = self.summary_toggle.isChecked()
        self.notes.setVisible(bool(notes) and expanded)
        self.run_button.setEnabled(not problems and not self._running())
        self.preview.setText(launch.preview(config, self.inventory, self.core))
        self.preview.setVisible(self._developer and expanded)
        self.summary.setVisible(expanded)

        self._update_dirty(config)
        self.settings.save_launch_state(config)
        self.state_changed.emit()

    def _running(self):
        return getattr(self, "_is_running", False)

    def _environment_changed(self, _text):
        if self._building:
            return
        # Keep whatever is still available; an account only in the old
        # environment cannot be part of this run any more.
        self._populate_users(self.users_list.checked())
        self._populate_server_logs(self.logs_list.checked())
        self._changed()

    def _users_mode_changed(self):
        if not self._building and self.users_mode.current() == "Choose accounts" \
                and not self.users_list.checked():
            self.users_list.set_all(True)     # "choose" starts from everything
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

    def _scenario_mode_changed(self, _id, checked):
        if checked:
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

    # -- context --------------------------------------------------------------
    def set_core(self, core):
        self.core = core
        self._changed()

    def set_inventory(self, inventory):
        """Repopulate every list from what the core says exists."""
        self.inventory = inventory
        config = self.state()
        self._building = True
        try:
            alias = config["environment"]
            self.env_combo.blockSignals(True)
            self.env_combo.clear()
            self.env_combo.addItem(ALL_ENVIRONMENTS)
            for env in inventory.envs:
                self.env_combo.addItem(env.get("alias", ""))
            self._select_env(alias)
            self.env_combo.blockSignals(False)

            self._populate_users(config["users"]["logins"])
            self._populate_extensions(config["extensions"]["names"])
            self._populate_server_logs(config["server_logs"]["names"])
            self._populate_scenarios(config["scenarios"]["selected"])
            self._populate_artifacts(config["reports"]["artifacts"])
            self._populate_overlay(config["overlay"]["components"])

            for key, name in (("reports_dir", "reports"),
                              ("sessions_dir", "sessions")):
                edit = self.dir_edits[key]
                # The Environments page owns the defaults; adopt them silently
                # while the user has not typed one of their own.
                stored = self.settings.directory(name)
                if stored and not edit.text():
                    edit.setText(stored)
                if not edit.text():
                    edit.setPlaceholderText(inventory.dirs().get(name) or "(default)")
        finally:
            self._building = False
        self._update_env_note()
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
        alias = self.env_combo.currentText()
        alias = "" if alias == ALL_ENVIRONMENTS else alias
        rows = []
        if self.inventory:
            env_value = self.inventory.env_value(alias) if alias else None
            for user in self.inventory.users:
                if env_value is not None and user.get("env") != env_value:
                    continue
                note = user.get("class", "")
                if not alias:
                    note = "%s   %s" % (user.get("env", ""), note)
                rows.append((user.get("login", ""), note))
        known = {login for login, _note in rows}
        for login in keep or ():
            if login not in known:
                rows.append((login, "(not in this environment)"))
        self.users_list.clear()
        for login, note in rows:
            self.users_list.add(login, login, note)
        self.users_list.set_checked(keep)
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

    def _populate_overlay(self, keep):
        self.overlay_list.clear()
        choices = (self.inventory.choices("overlay_components", launch.ALL_OVERLAY)
                   if self.inventory else launch.ALL_OVERLAY)
        for choice in choices:
            self.overlay_list.add(choice, choice)
        self.overlay_list.set_checked(keep)

    def _select_env(self, alias):
        """Put ``alias`` in the environment combo, adding it if it is not offered.

        The same rule the accounts, extensions and scenario lists already follow:
        a configured value the inventory does not have is kept, not dropped,
        because dropping it silently rewrites a saved configuration. Here it also
        has to be kept for a second reason - the combo is filled from --describe,
        which answers ~100ms after the page is built, so at restore time every
        environment is "not offered". setCurrentText on a non-editable QComboBox
        is a no-op when the item is absent, which is what made the choice vanish
        between one run of the app and the next.
        """
        if not alias:
            self.env_combo.setCurrentIndex(0)          # All environments
            return
        if self.env_combo.findText(alias) < 0:
            self.env_combo.addItem(alias)
        self.env_combo.setCurrentText(alias)

    def _update_env_note(self):
        if not self.inventory:
            self.env_note.setText("")
            return
        alias = self.env_combo.currentText()
        if alias == ALL_ENVIRONMENTS:
            self.env_note.setText("%d environments · %d accounts"
                                  % (len(self.inventory.envs),
                                     len(self.inventory.users)))
            return
        for env in self.inventory.envs:
            if env.get("alias") == alias:
                self.env_note.setText("%s · %s accounts"
                                      % (env.get("origin") or "no address",
                                         env.get("count", "?")))
                return
        # Kept, because it is what the configuration says (see _select_env), but
        # the config no longer has it - so say so rather than leave a blank line
        # under an environment that would launch nothing.
        self.env_note.setText("Not in users.json any more · 0 accounts")

    # -- unsaved changes ------------------------------------------------------
    # A saved configuration is a file, and the controls are a working copy of it.
    # Nothing writes back on its own - a run does not save, and switching away
    # would drop the edit - so the page has to say when the two have parted: both
    # Save buttons go red the moment the working copy stops matching what was
    # opened, and go quiet again the moment it is written or another one is
    # opened.
    #
    # "(unsaved)" is the same story with no file at the end of it: the settings
    # being built there are not stored anywhere, so an edit is flagged just the
    # same, and Save - which asks for a name - is the thing that settles it. The
    # baseline is taken when the page arrives at (unsaved) rather than being the
    # defaults, so opening the app to yesterday's restored settings is quiet and
    # only what happens next is flagged.

    @staticmethod
    def _comparable(config):
        """A configuration reduced to what "the same settings" means.

        Compared as text, through :func:`launch.merged`, so a file written before
        a key existed does not read as an edit; the lists are sorted because the
        order the tick boxes happen to be in is not part of the configuration -
        the inventory arriving can reorder them without anything having changed.
        """
        def norm(value):
            if isinstance(value, dict):
                return {key: norm(value[key]) for key in sorted(value)}
            if isinstance(value, list):
                return sorted(str(item) for item in value)
            return value

        return json.dumps(norm(launch.merged(config)), sort_keys=True)

    def is_dirty(self):
        """Whether the controls have moved away from the saved configuration."""
        return self._dirty

    def _mark_clean(self, config=None):
        """What is on screen now is the thing to measure later edits against."""
        self._baseline = self._comparable(config if config is not None
                                          else self.state())
        self._update_dirty()

    def _update_dirty(self, config=None):
        name = self._current_config_name()
        dirty = self._baseline is not None and \
            self._comparable(config if config is not None else self.state()) \
            != self._baseline
        self._dirty = dirty
        tip = ("Unsaved changes to \"%s\"" % name if name else
               "These settings are not saved as a configuration")
        for button in (self.config_save, self.footer_save):
            button.setProperty("dirty", "true" if dirty else "false")
            button.setToolTip(tip if dirty else "")
            # A stylesheet rule keyed on a property is only re-read when the
            # widget is re-polished; without this the button keeps the colour it
            # was built with.
            button.style().unpolish(button)
            button.style().polish(button)
            
        # A desktop link points at a saved configuration by name, so there has to
        # be one, and it has to match what is on screen. A checkable menu entry
        # draws its own tick, so whether a link exists needs no wording.
        is_saved = not dirty and bool(name)
        self.desktop_link.setEnabled(is_saved)
        self.desktop_link.setChecked(
            is_saved and os.path.exists(self._desktop_link_path(name)))

    # -- saved configurations -------------------------------------------------
    def _reload_configs(self, select=None):
        self.config_combo.blockSignals(True)
        self.config_combo.clear()
        self.config_combo.addItem(UNSAVED)
        for name in self._configs.names():
            self.config_combo.addItem(name)
        target = select if select is not None else self.settings.launch_config_name
        index = self.config_combo.findText(target or UNSAVED)
        self.config_combo.setCurrentIndex(index if index >= 0 else 0)
        self.config_combo.blockSignals(False)

    def _current_config_name(self):
        name = self.config_combo.currentText()
        return "" if name == UNSAVED else name

    def _config_selected(self, name):
        if self._building:
            return
        self.settings.launch_config_name = "" if name == UNSAVED else name
        if name and name != UNSAVED:
            saved = self._configs.get(name)
            if saved is not None:
                self.set_state(saved)
        self._mark_clean()

    def save_configuration(self):
        name = self._current_config_name()
        if not name:
            name, ok = QInputDialog.getText(self, "Save configuration",
                                            "Name this configuration:")
            name = (name or "").strip()
            if not ok or not name:
                return
            name = self._configs.unique_name(name)
        state = self.state()
        self._configs.put(name, state)
        self.settings.launch_config_name = name
        self._reload_configs(select=name)
        self._mark_clean(state)

    def duplicate_configuration(self):
        base = self._current_config_name() or "Launch configuration"
        name = self._configs.unique_name(base)
        state = self.state()
        self._configs.put(name, state)
        self.settings.launch_config_name = name
        self._reload_configs(select=name)
        self._mark_clean(state)

    def rename_configuration(self):
        old = self._current_config_name()
        if not old:
            QMessageBox.information(self, "Rename",
                                    "Save this configuration first.")
            return
        new, ok = QInputDialog.getText(self, "Rename configuration",
                                       "New name:", text=old)
        new = (new or "").strip()
        if not ok or not new or new == old:
            return
        
        # Keep desktop link if it existed for the old name
        old_path = self._desktop_link_path(old)
        had_link = os.path.exists(old_path)
        if had_link:
            os.remove(old_path)
            
        links = self.settings.desktop_links()
        if old in links:
            links[new] = links.pop(old)
            self.settings.save_desktop_links(links)

        self._configs.rename(old, self._configs.unique_name(new))
        self.settings.launch_config_name = new
        self._reload_configs(select=new)
        # The settings did not move, only the name they are filed under: an edit
        # that was unsaved before the rename is still unsaved after it.
        self._update_dirty()
        
        if had_link:
            self.desktop_link.setChecked(True)
            self._toggle_desktop_link()

    def _desktop_link_path(self, name):
        links = self.settings.desktop_links()
        if name in links and "path" in links[name]:
            return links[name]["path"]
        safe_name = name.replace(" ", "_").replace("/", "_")
        return os.path.expanduser(f"~/Desktop/QAVector_{safe_name}.desktop")

    def _toggle_desktop_link(self):
        name = self._current_config_name()
        if not name:
            return
            
        links = self.settings.desktop_links()
        current = links.get(name, {})
            
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
            content = f"""[Desktop Entry]
Version=1.0
Type=Application
Name={custom_name}
Exec={sys.executable} "{bootstrap_py}" --launch "{name}"
Terminal=false
Icon={custom_icon}
"""
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            os.chmod(path, 0o755)
            
            import subprocess
            import shutil
            if shutil.which("gio"):
                subprocess.run(["gio", "set", path, "metadata::trusted", "true"], capture_output=True)
                
            links[name] = {"path": path, "name": custom_name, "icon": custom_icon}
            self.settings.save_desktop_links(links)
            self._update_dirty()
        else:
            path = current.get("path")
            if not path:
                path = self._desktop_link_path(name)
            if path and os.path.exists(path):
                answer = QMessageBox.question(
                    self, "Remove Desktop Link",
                    "Are you sure you want to remove the desktop link?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if answer != QMessageBox.Yes:
                    self.desktop_link.setChecked(True)
                    return
                os.remove(path)
            if name in links:
                del links[name]
                self.settings.save_desktop_links(links)
            self._update_dirty()

    def delete_configuration(self):
        name = self._current_config_name()
        if not name:
            return
        answer = QMessageBox.question(
            self, "Delete configuration",
            "Delete \"%s\"? The current settings stay as they are." % name,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
            
        path = self._desktop_link_path(name)
        if os.path.exists(path):
            os.remove(path)
            
        links = self.settings.desktop_links()
        if name in links:
            del links[name]
            self.settings.save_desktop_links(links)
            
        self._configs.remove(name)
        self.settings.launch_config_name = ""
        self._reload_configs(select=UNSAVED)
        self._mark_clean()

    # -- helpers --------------------------------------------------------------
    def reset(self):
        self.set_state(launch.DEFAULTS)
        self.config_combo.setCurrentIndex(0)

    def _browse(self, edit):
        start = edit.text() or os.path.expanduser("~")
        chosen = QFileDialog.getExistingDirectory(self, "Choose a folder", start)
        if chosen:
            edit.setText(chosen)

    def _restore(self):
        saved = self.settings.launch_state()
        self.set_state(saved if saved else launch.DEFAULTS)
        # A named configuration is measured against its file, so an edit left
        # unsaved when the window closed is still an unsaved edit when it comes
        # back. With no name there is no file, and the restored settings are
        # where this session starts from.
        name = self._current_config_name()
        stored = self._configs.get(name) if name else None
        self._baseline = self._comparable(stored if stored is not None
                                          else self.state())
        self._update_dirty()
