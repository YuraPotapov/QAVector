"""History: what was run, how it ended, and how to run it again.

This is deliberately not a log viewer. A log tells you what happened; an entry
here carries the configuration that caused it, so the useful verb is not "read"
but "run again" - either straight away, or after opening it back up in the page
that produced it and changing one thing.

Both kinds live in the same table because the question a user asks is "what did
I run on Tuesday", not "which interface did I use" - which is also why the
filters are about what happened (how it ended, where, and when) rather than
about which page it came from. What differs is where an entry goes back to: a
Launch Sessions entry restores its configuration, a Command entry its form.
"""

import datetime
import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QHeaderView, QLabel, QMessageBox,
                               QPushButton, QSplitter, QStackedWidget, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from .. import commands, history as history_mod, icons, launch, theme, widgets

HEADERS = ["When", "Type", "Environment", "Users", "Scenarios", "Result", "Took"]

KIND_LABELS = {history_mod.LAUNCH: "Launch", history_mod.COMMAND: "Command"}
STATUS_VARIANTS = {history_mod.OK: "accent", history_mod.FAILED: "bad",
                   history_mod.STOPPED: "warn", history_mod.ERROR: "bad",
                   history_mod.RUNNING: "outline"}
STATUS_COLORS = {history_mod.OK: theme.OK, history_mod.FAILED: theme.BAD,
                 history_mod.STOPPED: theme.WARN, history_mod.ERROR: theme.BAD,
                 history_mod.RUNNING: theme.ACCENT}

RESULTS = ["All", "Passed", "Failed", "Stopped"]
#: The statuses each Result choice keeps. Failed takes error too: read from the
#: list both mean "this did not pass", and the row itself says which it was. A
#: run still in flight has no result yet, so only All shows it.
RESULT_STATUSES = {"Passed": {history_mod.OK},
                   "Failed": {history_mod.FAILED, history_mod.ERROR},
                   "Stopped": {history_mod.STOPPED}}

PERIODS = ["All time", "Today", "7 days"]
#: How far back each Period reaches, in days; Today is the calendar day.
PERIOD_DAYS = {"Today": 0, "7 days": 7}

ALL_ENVIRONMENTS = "All environments"
#: A run that was not pointed at one environment - the launcher's "all of them".
EVERY_ENVIRONMENT = "Every environment"


class HistoryPage(QWidget):
    """The run record, with the actions that make it reusable."""

    rerun_requested = Signal(dict)
    restore_requested = Signal(dict)
    open_log_requested = Signal(str)
    open_artifacts_requested = Signal(str)

    def __init__(self, history, parent=None):
        super().__init__(parent)
        self.history = history
        self._rows = []

        column = QVBoxLayout(self)
        column.setContentsMargins(24, 20, 24, 20)
        column.setSpacing(6)
        column.addWidget(widgets.heading("History"))
        column.addWidget(widgets.lede(
            "Every run started from here. Pick one to see what it was asked to do, "
            "open what it produced, or run it again."))
        column.addSpacing(12)

        self.result_filter = widgets.Segmented(RESULTS, "All")
        self.result_filter.changed.connect(lambda _v: self.refresh())
        self.environment_filter = QComboBox()
        self.environment_filter.addItem(ALL_ENVIRONMENTS, None)
        self.environment_filter.currentIndexChanged.connect(lambda _i: self.refresh())
        self.period_filter = widgets.Segmented(PERIODS, "All time")
        self.period_filter.changed.connect(lambda _v: self.refresh())
        self.count = widgets.mono("")
        clear = QPushButton("Clear history")
        clear.clicked.connect(self.clear_history)
        column.addWidget(widgets.row(self.result_filter, 12, self.environment_filter,
                                     12, self.period_filter, None, self.count, clear))
        column.addSpacing(8)

        splitter = QSplitter(Qt.Vertical)
        table_panel = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        header = self.table.horizontalHeader()
        for index in range(len(HEADERS)):
            header.setSectionResizeMode(index, QHeaderView.Interactive)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.doubleClicked.connect(lambda _i: self.restore_selected())
        table_panel.layout().addWidget(self.table)
        splitter.addWidget(table_panel)

        splitter.addWidget(self._detail_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        column.addWidget(splitter, 1)

        self.history.changed.connect(self.refresh)
        self.refresh()

    def _detail_panel(self):
        panel = widgets.BlueprintPanel()
        self.detail_stack = QStackedWidget()

        # Nothing selected is a state, not a run with its text missing, so it is
        # said on its own ground. It used to be a heading beside the status pill
        # left blank - and an empty bordered pill reads as a disabled checkbox.
        empty = QWidget()
        empty_column = QVBoxLayout(empty)
        empty_column.setContentsMargins(0, 0, 0, 0)
        self.empty_state = widgets.empty_zone(
            "No run selected",
            "Pick a run above to see what it was asked to do, open its log or "
            "artifacts, or run it again.")
        empty_column.addWidget(self.empty_state)
        empty_column.addStretch(1)
        self.detail_stack.addWidget(empty)

        details = QWidget()
        detail_column = QVBoxLayout(details)
        detail_column.setContentsMargins(0, 0, 0, 0)
        detail_column.setSpacing(6)
        self.detail_title = widgets.heading("", "h2")
        self.status_tag = widgets.Tag("", "neutral")
        detail_column.addWidget(widgets.row(self.detail_title, self.status_tag, None))

        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.detail.setStyleSheet("font-size: 12px; color: %s;" % theme.NEUTRAL[800])
        detail_column.addWidget(self.detail)
        detail_column.addStretch(1)

        self.rerun_button = icons.button(QPushButton("Run again"), "run")
        self.rerun_button.setProperty("variant", "primary")
        self.rerun_button.clicked.connect(self.rerun_selected)
        self.restore_button = QPushButton("Open in page")
        self.restore_button.clicked.connect(self.restore_selected)
        self.log_button = icons.button(QPushButton("Open log"), "log")
        self.log_button.clicked.connect(self.open_log)
        self.artifacts_button = icons.button(QPushButton("Open artifacts"), "artifacts")
        self.artifacts_button.clicked.connect(self.open_artifacts)
        self.delete_button = QPushButton("Delete")
        self.delete_button.clicked.connect(self.delete_selected)
        detail_column.addWidget(widgets.row(
            self.rerun_button, self.restore_button, widgets.vline(),
            self.log_button, self.artifacts_button, None, self.delete_button))
        self.detail_stack.addWidget(details)

        panel.layout().addWidget(self.detail_stack)
        self._enable_actions(None)
        return panel

    # -- data -----------------------------------------------------------------
    def filters(self):
        """``(result, environment, period)`` as chosen; environment None is any."""
        return (self.result_filter.current(), self.environment_filter.currentData(),
                self.period_filter.current())

    def refresh(self):
        chosen = self.selected_entry()
        recorded = self.history.entries()
        self._fill_environments(recorded)
        result, environment, period = self.filters()
        now = datetime.datetime.now()
        self._rows = [entry for entry in recorded
                      if matches(entry, result, environment, period, now)]
        self.table.setRowCount(len(self._rows))
        for index, entry in enumerate(self._rows):
            self._fill_row(index, entry)
        self.table.resizeColumnsToContents()
        self.count.setText("%d shown of %d recorded" % (len(self._rows), len(recorded)))
        if chosen is not None:
            self._select_id(chosen.get("id"))
        self._selection_changed()

    def _fill_environments(self, entries):
        """Offer the environments the record actually has, keeping the choice.

        Read off the entries rather than the inventory: a run against an
        environment since removed from users.json is still history, and one the
        record has never seen would only ever filter down to nothing. A choice
        whose last entry was deleted falls back to All.
        """
        combo = self.environment_filter
        current = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(ALL_ENVIRONMENTS, None)
        for alias in sorted({entry_environment(entry) for entry in entries}):
            combo.addItem(alias or EVERY_ENVIRONMENT, alias)
        index = combo.findData(current) if current is not None else 0
        combo.setCurrentIndex(max(index, 0))
        combo.blockSignals(False)

    def _fill_row(self, index, entry):
        status = entry.get("status", "")
        self._set(index, 0, entry.get("started_at", ""), mono=True)
        self._set(index, 1, KIND_LABELS.get(entry.get("kind"), entry.get("kind", "")),
                  color=theme.ACCENT_RAMP[700])
        environment, users, scenarios = describe_entry(entry)
        self._set(index, 2, environment)
        self._set(index, 3, users)
        self._set(index, 4, scenarios)
        self._set(index, 5, result_text(entry), mono=True,
                  color=STATUS_COLORS.get(status, theme.NEUTRAL[700]))
        self._set(index, 6, history_mod.format_duration(entry.get("duration_ms")),
                  mono=True)
        self.table.item(index, 0).setData(Qt.UserRole, entry.get("id"))

    def _set(self, row, col, text, mono=False, color=None):
        item = QTableWidgetItem(str(text or ""))
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        if mono:
            item.setFont(theme.mono_font(9))
        if color:
            item.setForeground(theme.color(color))
        self.table.setItem(row, col, item)

    def _select_id(self, entry_id):
        for index, entry in enumerate(self._rows):
            if entry.get("id") == entry_id:
                self.table.selectRow(index)
                return

    def selected_entry(self):
        rows = {i.row() for i in self.table.selectedIndexes()}
        if len(rows) != 1:
            return None
        row = rows.pop()
        return self._rows[row] if 0 <= row < len(self._rows) else None

    # -- detail ---------------------------------------------------------------
    def _selection_changed(self):
        entry = self.selected_entry()
        self._enable_actions(entry)
        if entry is None:
            self.detail_stack.setCurrentIndex(0)
            return
        self.detail_stack.setCurrentIndex(1)
        self.detail_title.setText("%s · %s" % (
            KIND_LABELS.get(entry.get("kind"), "Run"), entry.get("started_at", "")))
        status = entry.get("status", "")
        self.status_tag.set(result_text(entry),
                            STATUS_VARIANTS.get(status, "neutral"))
        self.detail.setText("\n".join("%s: %s" % (label, value)
                                      for label, value in detail_rows(entry)))
        self.restore_button.setText(
            "Open in Launch Sessions" if entry.get("kind") == history_mod.LAUNCH
            else "Open in Command")

    def _enable_actions(self, entry):
        has = entry is not None
        for button in (self.rerun_button, self.restore_button, self.delete_button):
            button.setEnabled(has)
        log = (entry or {}).get("log_file") or ""
        run_dir = (entry or {}).get("run_dir") or ""
        self.log_button.setEnabled(bool(log) and os.path.isfile(log))
        self.artifacts_button.setEnabled(bool(run_dir) and os.path.isdir(run_dir))

    # -- actions --------------------------------------------------------------
    def rerun_selected(self):
        entry = self.selected_entry()
        if entry is not None:
            self.rerun_requested.emit(entry)

    def restore_selected(self):
        entry = self.selected_entry()
        if entry is not None:
            self.restore_requested.emit(entry)

    def open_log(self):
        entry = self.selected_entry()
        if entry and entry.get("log_file"):
            self.open_log_requested.emit(entry["log_file"])

    def open_artifacts(self):
        entry = self.selected_entry()
        if entry and entry.get("run_dir"):
            self.open_artifacts_requested.emit(entry["run_dir"])

    def delete_selected(self):
        entry = self.selected_entry()
        if entry is None:
            return
        self.history.remove(entry.get("id"))

    def clear_history(self):
        if not self.history.entries():
            return
        answer = QMessageBox.question(
            self, "Clear history",
            "Delete every recorded run and its saved log?\n\n"
            "Reports the launcher wrote to disk are not touched.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            self.history.clear()


# -- filtering ----------------------------------------------------------------
def entry_environment(entry):
    """The environment alias a run was pointed at; "" for every environment."""
    if entry.get("kind") == history_mod.LAUNCH:
        return (entry.get("launch_config") or {}).get("environment") or ""
    return (entry.get("command_state") or {}).get("--env") or ""


def matches(entry, result="All", environment=None, period="All time", now=None):
    """Whether ``entry`` passes the page's three filters.

    ``result`` and ``period`` are the choices as labelled (see RESULTS and
    PERIODS); ``environment`` is an alias, "" meaning a run against every
    environment, and None meaning any at all.
    """
    statuses = RESULT_STATUSES.get(result)
    if statuses is not None and entry.get("status") not in statuses:
        return False
    if environment is not None and entry_environment(entry) != environment:
        return False
    days = PERIOD_DAYS.get(period)
    if days is not None:
        started = _started(entry)
        if started is None:
            return False
        now = now or datetime.datetime.now()
        if days == 0:
            return started.date() == now.date()
        return started >= now - datetime.timedelta(days=days)
    return True


def _started(entry):
    try:
        return datetime.datetime.fromisoformat(entry.get("started_at") or "")
    except (TypeError, ValueError):
        return None


# -- how an entry reads -------------------------------------------------------
def describe_entry(entry):
    """``(environment, users, scenarios)`` for the table, whatever the kind."""
    if entry.get("kind") == history_mod.LAUNCH:
        config = entry.get("launch_config") or {}
        return (launch.env_label(config), launch.users_label(config),
                launch.scenarios_label(config))
    state = entry.get("command_state") or {}
    return (state.get("--env") or "every environment",
            state.get("--filter-users") or state.get("--user") or "all accounts",
            state.get("--run-tests") or "just open the windows")


def result_text(entry):
    """"passed", or "failed · exit 1", or "4/7 passed"."""
    status = entry.get("status", "")
    label = history_mod.STATUS_LABELS.get(status, status)
    total = entry.get("total") or 0
    if total and status in (history_mod.OK, history_mod.FAILED):
        return "%s · %d/%d" % (label, entry.get("passed") or 0, total)
    code = entry.get("exit_code")
    if status in (history_mod.FAILED, history_mod.ERROR) and code is not None:
        return "%s · exit %s" % (label, code)
    return label


def detail_rows(entry):
    """``[(label, value)]`` describing what the run was asked to do."""
    if entry.get("kind") == history_mod.LAUNCH:
        rows = launch.summarise(entry.get("launch_config") or {})
    else:
        rows = _command_rows(entry.get("command_state") or {})
    # Named for the folder, not for what it holds: "Reports" is already a row
    # above, describing which artifacts were asked for.
    if entry.get("run_dir"):
        rows.append(("Artifacts folder", entry["run_dir"]))
    if entry.get("log_file"):
        rows.append(("Saved log", entry["log_file"]))
    return rows


def _command_rows(state):
    """Only the flags that were actually set - the form as it was submitted."""
    rows = []
    for flag in commands.FLAGS:
        value = state.get(flag.name)
        if flag.kind == "flag":
            if value:
                rows.append((flag.name, "on"))
            continue
        text = ",".join(value) if isinstance(value, (list, tuple)) else str(value or "")
        if text.strip() and text.strip() != (flag.default or ""):
            rows.append((flag.name, text.strip()))
    return rows or [("Command", "every flag left at its default")]
