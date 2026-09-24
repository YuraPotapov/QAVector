"""Scenarios: see every one, edit it, and make new ones.

Scenarios are what this application is for, and until now they could only be
run. Writing one meant knowing the step grammar, which named selectors exist and
which blocks there are to ``use:`` - so they were written in a text editor by
whoever already knew, and nobody else.

Two views of the same file, because there are two ways to work on one. **Steps**
is a row per step, for reordering and for the ordinary business of changing what
a step points at. **YAML** is the file itself, because the grammar has corners a
form should not try to grow a widget for - ``retry:``, ``{{env.origin}}``,
composition through ``use:`` - and hiding them would make the editor a worse tool
than the text editor it replaces. Whichever view was last edited is the one that
gets saved.

Nothing here parses YAML. The GUI depends on PySide6 and nothing else, so the
file format stays in the core and this page talks to it over --flow-show /
--flow-save / --flow-delete / --flow-import, the same way it reads --describe.
"""

import json
import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QComboBox, QFileDialog, QHeaderView, QInputDialog,
                               QLabel, QLineEdit, QPlainTextEdit,
                               QPushButton, QSplitter, QStackedWidget, QTableWidget,
                               QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from .. import core as core_mod, theme, widgets

#: Fallback for a core too old to advertise its grammar in --describe.
FALLBACK_ACTIONS = {
    "selector_only": ["click", "wait_for", "assert_exists", "assert_visible",
                      "assert_not_visible"],
    "selector_and_value": ["fill", "select", "assert_text_contains"],
    "value_only": ["assert_url_contains", "assert_title", "assert_host_up", "press"],
    "url_target": ["goto"],
    "service_target": ["service_start", "service_stop", "service_restart",
                       "wait_for_service"],
    "service_target_and_value": ["wait_for_out", "wait_for_criterion"],
    "use": ["use"],
    "states": ["visible", "attached", "hidden", "detached"],
}

STEP_HEADERS = ["Action", "Target", "Value", "Timeout", "Resolves to"]
# Tags are deliberately not a column: at this width they cost the name
# every character it has, and the search box already matches on them.
LIST_HEADERS = ["Flow", "Name", ""]
SELECTOR_HEADERS = ["Name", "Selector", ""]

#: Column of the steps table that shows what a named target resolves to. Filled
#: from --describe and never editable: it is what selectors.yaml says, and the
#: place to change it is the Selectors tab.
RESOLVED_COLUMN = 4


class ScenariosPage(QWidget):
    """List on the left, editor on the right."""

    #: A scenario was written or removed; the window re-reads --describe.
    saved = Signal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.core = None
        self.inventory = core_mod.Inventory()
        self.current = None          # the --flow-show payload being edited
        self._baseline = None
        self._dirty = False
        self._building = False
        self._last_edited = "steps"  # which view Save should believe
        self._recorded = []          # steps seen on the --events stream, live
        self._open_when_listed = ""  # a recording to open once --describe has it
        self._developer = False      # which register the lede is written in
        self._flows_dir = ""

        column = QVBoxLayout(self)
        column.setContentsMargins(24, 20, 24, 20)
        column.setSpacing(6)

        self.new_button = QPushButton("New")
        self.new_button.clicked.connect(self.new_scenario)
        self.duplicate_button = QPushButton("Duplicate")
        self.duplicate_button.clicked.connect(self.duplicate_scenario)
        self.import_button = QPushButton("Import…")
        self.import_button.clicked.connect(self.import_scenario)
        self.export_button = QPushButton("Export…")
        self.export_button.clicked.connect(self.export_scenario)
        self.delete_button = QPushButton("Delete")
        self.delete_button.clicked.connect(self.delete_scenario)
        self.revert_button = QPushButton("Revert")
        self.revert_button.setToolTip("Throw away the edits and re-read the file")
        self.revert_button.clicked.connect(self.revert)
        self.save_button = QPushButton("Save")
        self.save_button.setProperty("variant", "primary")
        self.save_button.clicked.connect(self.save)
        column.addWidget(widgets.row(
            widgets.heading("Scenarios"), None, self.new_button,
            self.duplicate_button, self.import_button, self.export_button,
            self.delete_button, self.revert_button, self.save_button))

        self.lede = widgets.lede("")
        column.addWidget(self.lede)
        column.addSpacing(12)

        self.left = QTabWidget()
        self.left.addTab(self._list_panel(), "Flows")
        self.left.addTab(self._selector_list_panel(), "Selectors")
        self.left.currentChanged.connect(self._left_tab_changed)

        self.editors = QStackedWidget()
        self.editors.addWidget(self._editor_panel())
        self.editors.addWidget(self._selector_editor_panel())

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.left)
        splitter.addWidget(self.editors)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([430, 950])
        column.addWidget(splitter, 1)

        self.status = widgets.mono("")
        column.addWidget(self.status)
        self._update_buttons()

    # -- construction ---------------------------------------------------------
    def _list_panel(self):
        panel = widgets.BlueprintPanel(padding=(12, 12, 12, 12))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search scenarios and tags…")
        self.search.textChanged.connect(self._apply_filter)
        panel.layout().addWidget(self.search)

        self.table = QTableWidget(0, len(LIST_HEADERS))
        self.table.setHorizontalHeaderLabels(LIST_HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        header = self.table.horizontalHeader()
        # The id is what everything else refers to - --run-tests, `use:`, the
        # file name - so it gets enough room for a long one before the name,
        # which is prose and can be read at whatever width is left.
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.resizeSection(0, 190)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        panel.layout().addWidget(self.table, 1)
        panel.layout().addWidget(widgets.lede(
            "Scenarios run on their own. Blocks are the pieces a scenario reaches "
            "through use:, and open here too."))
        return panel

    def _selector_list_panel(self):
        """Every named target, and what it looks for."""
        panel = widgets.BlueprintPanel(padding=(12, 12, 12, 12))
        self.selector_search = QLineEdit()
        self.selector_search.setPlaceholderText("Search names and selectors…")
        self.selector_search.textChanged.connect(self._filter_selectors)
        panel.layout().addWidget(self.selector_search)

        self.selector_table = QTableWidget(0, len(SELECTOR_HEADERS))
        self.selector_table.setHorizontalHeaderLabels(SELECTOR_HEADERS)
        self.selector_table.verticalHeader().setVisible(False)
        self.selector_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.selector_table.setSelectionMode(QTableWidget.SingleSelection)
        self.selector_table.setEditTriggers(QTableWidget.NoEditTriggers)
        header = self.selector_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.resizeSection(0, 180)
        panel.layout().addWidget(self.selector_table, 1)

        self.override_button = QPushButton("Add to my selectors")
        self.override_button.clicked.connect(self.override_selector)
        panel.layout().addWidget(widgets.row(self.override_button, None))
        panel.layout().addWidget(widgets.lede(
            "Flows point at these names, so changing one here changes every flow "
            "that uses it. The ones the app ships are read-only; adding a name of "
            "your own with the same spelling overrides it."))
        return panel

    def _selector_editor_panel(self):
        panel = widgets.BlueprintPanel(padding=(12, 12, 12, 12))
        self.selector_title = widgets.heading("Your selectors", "h2")
        self.selector_tag = widgets.Tag("", "neutral")
        panel.layout().addWidget(widgets.row(self.selector_title, None,
                                             self.selector_tag))
        self.selector_path = widgets.elided_mono("")
        panel.layout().addWidget(self.selector_path)
        panel.layout().addWidget(widgets.lede(
            "One name per line, as name: \"selector\". Playwright syntax is "
            "allowed - :has-text(), :nth-match() - but prefer something "
            "structural: a flow that keys on a visible label breaks the moment "
            "the same app is opened in another language."))

        self.selector_yaml = QPlainTextEdit()
        self.selector_yaml.setFont(theme.mono_font(10))
        self.selector_yaml.textChanged.connect(self._selector_yaml_edited)
        panel.layout().addWidget(self.selector_yaml, 1)

        self.selector_problems = QLabel("")
        self.selector_problems.setWordWrap(True)
        panel.layout().addWidget(self.selector_problems)

        self.selector_save = QPushButton("Save selectors")
        self.selector_save.setProperty("variant", "primary")
        self.selector_save.clicked.connect(self.save_selectors)
        self.selector_revert = QPushButton("Revert")
        self.selector_revert.clicked.connect(self.load_selectors)
        panel.layout().addWidget(widgets.row(None, self.selector_revert,
                                             self.selector_save))
        return panel

    def _editor_panel(self):
        panel = widgets.BlueprintPanel(padding=(12, 12, 12, 12))

        self.title = widgets.heading("", "h2")
        self.source_tag = widgets.Tag("", "neutral")
        panel.layout().addWidget(widgets.row(self.title, None, self.source_tag))
        self.path_label = widgets.elided_mono("")
        panel.layout().addWidget(self.path_label)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("what this scenario checks")
        self.name_edit.textChanged.connect(self._changed)
        panel.layout().addWidget(widgets.field("Name", self.name_edit))

        self.tags_edit = QLineEdit()
        self.tags_edit.setProperty("mono", True)
        self.tags_edit.setPlaceholderText("smoke, access, template …")
        self.tags_edit.textChanged.connect(self._changed)
        panel.layout().addWidget(widgets.field(
            "Tags", self.tags_edit,
            "Comma-separated. template, manual and blocked are kept out of a "
            "run-everything selection; a new scenario starts as template."))

        self.tabs = QTabWidget()
        self.tabs.addTab(self._steps_tab(), "Steps")
        self.tabs.addTab(self._yaml_tab(), "YAML")
        panel.layout().addWidget(self.tabs, 1)

        self.problems = QLabel("")
        self.problems.setWordWrap(True)
        panel.layout().addWidget(self.problems)
        return panel

    def _steps_tab(self):
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 8, 0, 0)
        box.setSpacing(6)

        self.add_step_button = QPushButton("Add step")
        self.add_step_button.clicked.connect(self.add_step)
        self.remove_step_button = QPushButton("Remove")
        self.remove_step_button.clicked.connect(self.remove_step)
        self.up_button = QPushButton("Move up")
        self.up_button.clicked.connect(lambda: self.move_step(-1))
        self.down_button = QPushButton("Move down")
        self.down_button.clicked.connect(lambda: self.move_step(1))
        # A step's target is an alias - another flow's id, or a name from
        # selectors.yaml - so there has to be a way through to the thing itself.
        self.open_target_button = QPushButton("Open target")
        self.open_target_button.setToolTip(
            "Open the block a use: step names, or show the selector a target resolves to")
        self.open_target_button.clicked.connect(self.open_target)
        box.addWidget(widgets.row(self.add_step_button, self.remove_step_button,
                                  self.up_button, self.down_button,
                                  self.open_target_button, None))

        self.steps = QTableWidget(0, len(STEP_HEADERS))
        self._editor_height = None        # measured once, see _fit_step_rows
        self.steps.setHorizontalHeaderLabels(STEP_HEADERS)
        self.steps.verticalHeader().setVisible(False)
        self.steps.setSelectionBehavior(QTableWidget.SelectRows)
        self.steps.setSelectionMode(QTableWidget.SingleSelection)
        header = self.steps.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(RESOLVED_COLUMN, QHeaderView.Interactive)
        # Target is the field being edited and the one that varies most, so it
        # takes the slack; the rest get just enough to be read.
        header.resizeSection(0, 120)
        header.resizeSection(2, 100)
        header.resizeSection(RESOLVED_COLUMN, 200)
        self.steps.itemChanged.connect(self._step_edited)
        self.steps.itemDoubleClicked.connect(lambda _item: self.open_target())
        box.addWidget(self.steps, 1)
        return page

    def _yaml_tab(self):
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 8, 0, 0)
        self.yaml = QPlainTextEdit()
        self.yaml.setFont(theme.mono_font(10))
        self.yaml.setTabStopDistance(28)
        self.yaml.textChanged.connect(self._yaml_edited)
        box.addWidget(self.yaml)
        return page

    # -- context --------------------------------------------------------------
    def set_core(self, core):
        self.core = core

    def set_inventory(self, inventory):
        self.inventory = inventory
        self._reload_list()
        if self._open_when_listed:
            wanted, self._open_when_listed = self._open_when_listed, ""
            self.select(wanted)
        self._reload_selector_list()
        self._refresh_resolved()
        self._flows_dir = inventory.dirs().get("flows") or ""
        self._write_lede()

    def set_developer_mode(self, enabled):
        self._developer = bool(enabled)
        self._write_lede()

    def _write_lede(self):
        """Said two ways, and it carries a path, so it is built rather than paired.

        See widgets.Phrasing for the pairs that are only a string; the mode is
        the same one, and the flag is only an answer to someone who is going to
        type it.
        """
        self.lede.setText(
            "Everything %s can run, the blocks they are built from, and the named "
            "targets they point at. Yours are written to %s; the ones that ship "
            "with the application are read-only - duplicate one to change it."
            % ("--run-tests" if self._developer else "a run",
               self._flows_dir or "the flows directory"))

    def actions_for(self, group):
        """One group of the step grammar, from the core when it says so."""
        advertised = self.inventory.flow_actions()
        return list(advertised.get(group) or FALLBACK_ACTIONS.get(group, []))

    def all_actions(self):
        seen = []
        for group in ("use", "url_target", "selector_only", "selector_and_value",
                      "value_only", "service_target", "service_target_and_value"):
            for action in self.actions_for(group):
                if action not in seen:
                    seen.append(action)
        return seen

    # -- the list -------------------------------------------------------------
    def _reload_list(self):
        selected = self.current["id"] if self.current else None
        # Blocks after scenarios: they are not runnable, so they are the second
        # thing you are looking for, but `use: access.open_app` has to lead
        # somewhere or the step is an alias with nothing behind it.
        rows = ([dict(row, kind="scenario") for row in self.inventory.scenarios]
                + [dict(row, kind="block") for row in self.inventory.blocks])
        self._building = True
        try:
            self.table.setRowCount(len(rows))
            for index, row in enumerate(rows):
                self._set(index, 0, row.get("id", ""))
                self._set(index, 1, row.get("name") or "", mono=False)
                # Marks the exception, not the rule: most flows are bundled
                # scenarios, so those say nothing and the column stays narrow.
                # What is worth pointing at is "this one is yours to edit" and
                # "this one is a block, not something you can run".
                note = " · ".join(
                    ([] if row["kind"] == "scenario" else [row["kind"]])
                    + (["yours"] if row.get("writable", True) else []))
                self._set(index, 2, note, color=theme.NEUTRAL[600])
                # Tags have no column, so they ride along for the search box and
                # show up in a tooltip rather than being invisible.
                tags = ", ".join(row.get("tags") or [])
                for cell in range(3):
                    item = self.table.item(index, cell)
                    if item is not None:
                        item.setToolTip(tags)
                        item.setData(Qt.UserRole, tags)
        finally:
            self._building = False
        self._apply_filter()
        if selected:
            self.select(selected)

    def _reload_selector_list(self):
        entries = self.inventory.selectors
        own = set(self._selector_names(self.selector_yaml.toPlainText())) \
            if hasattr(self, "selector_yaml") else set()
        self._building = True
        try:
            self.selector_table.setRowCount(len(entries))
            for index, name in enumerate(sorted(entries)):
                item = QTableWidgetItem(name)
                item.setFont(theme.mono_font(9))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.selector_table.setItem(index, 0, item)
                value = QTableWidgetItem(str(entries[name]))
                value.setFont(theme.mono_font(9))
                value.setFlags(value.flags() & ~Qt.ItemIsEditable)
                # Selectors are long and the column is not: the whole thing has
                # to be readable somewhere, or the list only shows that a name
                # exists and never what it does.
                value.setToolTip(str(entries[name]))
                item.setToolTip(str(entries[name]))
                self.selector_table.setItem(index, 1, value)
                mark = QTableWidgetItem("yours" if name in own else "")
                mark.setFont(theme.mono_font(9))
                mark.setForeground(theme.color(theme.NEUTRAL[600]))
                mark.setFlags(mark.flags() & ~Qt.ItemIsEditable)
                self.selector_table.setItem(index, 2, mark)
        finally:
            self._building = False
        self._filter_selectors()

    @staticmethod
    def _selector_names(text):
        """The names defined in a selectors.yaml, without parsing YAML.

        Only used to mark rows as "yours", so a line-level reading is enough -
        and the GUI has no YAML parser to do better with.
        """
        names = []
        for line in (text or "").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or ":" not in line:
                continue
            if line[:1].strip():                      # top-level key, not nested
                names.append(line.split(":", 1)[0].strip())
        return names

    def _filter_selectors(self):
        needle = self.selector_search.text().strip().lower()
        for index in range(self.selector_table.rowCount()):
            haystack = " ".join(
                (self.selector_table.item(index, column).text()
                 if self.selector_table.item(index, column) else "")
                for column in range(2)).lower()
            self.selector_table.setRowHidden(
                index, bool(needle) and needle not in haystack)

    def _left_tab_changed(self, index):
        self.editors.setCurrentIndex(1 if index == 1 else 0)
        if index == 1 and not self.selector_yaml.toPlainText() and self.core:
            self.load_selectors()

    def _refresh_resolved(self):
        """Fill the "Resolves to" column from what selectors.yaml says."""
        if not hasattr(self, "steps"):
            return
        selectors = self.inventory.selectors
        self._building = True
        try:
            for index in range(self.steps.rowCount()):
                combo = self.steps.cellWidget(index, 0)
                action = combo.currentText() if combo is not None else ""
                target_item = self.steps.item(index, 1)
                target = target_item.text().strip() if target_item else ""
                if action in self.actions_for("use"):
                    resolved = "block"
                elif action in self.actions_for("url_target"):
                    resolved = "url"
                elif (action in self.actions_for("service_target")
                        or action in self.actions_for("service_target_and_value")):
                    # A Project/Service reference from services.json. Saying
                    # "raw selector" here would be the column lying about the
                    # one thing it is for.
                    resolved = "service"
                else:
                    resolved = selectors.get(target, "")
                    if not resolved and target:
                        resolved = "raw selector"
                item = QTableWidgetItem(resolved)
                item.setFont(theme.mono_font(9))
                item.setForeground(theme.color(theme.NEUTRAL[600]))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.steps.setItem(index, RESOLVED_COLUMN, item)
        finally:
            self._building = False

    def _set(self, row, column, text, mono=True, color=None):
        item = QTableWidgetItem(text)
        if mono:
            item.setFont(theme.mono_font(9))
        if color:
            item.setForeground(theme.color(color) if isinstance(color, str)
                               else color)
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        self.table.setItem(row, column, item)

    def _apply_filter(self):
        needle = self.search.text().strip().lower()
        for index in range(self.table.rowCount()):
            first = self.table.item(index, 0)
            haystack = " ".join(
                [(self.table.item(index, column).text()
                  if self.table.item(index, column) else "") for column in range(2)]
                + [(first.data(Qt.UserRole) or "") if first else ""]).lower()
            self.table.setRowHidden(index, bool(needle) and needle not in haystack)

    def selected_id(self):
        items = self.table.selectedItems()
        if not items:
            return ""
        return self.table.item(items[0].row(), 0).text()

    def select(self, flow_id):
        for index in range(self.table.rowCount()):
            item = self.table.item(index, 0)
            if item is not None and item.text() == flow_id:
                self.table.selectRow(index)
                return True
        return False

    def _selection_changed(self):
        if self._building:
            return
        flow_id = self.selected_id()
        if not flow_id or (self.current and self.current["id"] == flow_id):
            return
        if not self._confirm_discard():
            self._building = True
            try:
                self.select(self.current["id"] if self.current else "")
            finally:
                self._building = False
            return
        self.open(flow_id)

    # -- opening --------------------------------------------------------------
    def open(self, flow_id):
        """Load one scenario into the editor."""
        if not self.core:
            return
        try:
            payload = self.core.flow_show(flow_id)
        except core_mod.CoreError as exc:
            widgets.warn(self, "Scenarios", str(exc))
            return
        self.current = payload
        self._building = True
        try:
            meta = payload.get("meta") or {}
            self.title.setText(payload.get("id", ""))
            writable = payload.get("writable", False)
            self.source_tag.set("editable" if writable else "ships with the app",
                                "accent" if writable else "neutral")
            self.path_label.setText(payload.get("path", ""))
            self.name_edit.setText(meta.get("name") or "")
            self.tags_edit.setText(", ".join(meta.get("tags") or []))
            self._load_steps(payload.get("steps") or [])
            self.yaml.setPlainText(payload.get("yaml") or "")
            self._set_read_only(not writable)
        finally:
            self._building = False
        self._last_edited = "steps"
        self._refresh_resolved()
        self._show_problems(payload)
        self._mark_clean()

    def _load_steps(self, steps):
        self.steps.setRowCount(len(steps))
        for index, step in enumerate(steps):
            self._fill_step_row(index, step)

    def _fill_step_row(self, index, step):
        combo = QComboBox()
        combo.addItems(self.all_actions())
        action = step.get("action") or ""
        if action and combo.findText(action) < 0:
            combo.addItem(action)      # a step the core no longer knows: keep it
        combo.setCurrentText(action)
        combo.setFont(theme.mono_font(9))
        combo.currentTextChanged.connect(self._changed)
        self.steps.setCellWidget(index, 0, combo)
        self._fit_step_rows(combo)
        for column, value in ((1, step.get("target")), (2, step.get("value")),
                              (3, step.get("timeout"))):
            item = QTableWidgetItem("" if value is None else str(value))
            item.setFont(theme.mono_font(9))
            self.steps.setItem(index, column, item)

    def _fit_step_rows(self, combo):
        """Make every step row tall enough for the inputs that sit in it.

        Two of them: the action box, and the line edit a cell turns into when it
        is edited. The view insets both by the item padding above and below
        (theme.CELL_INSET_V), and neither draws shorter than its own minimum - so
        a row sized for text left both 3px down and hanging over the gridline
        beneath. The Services and Environments tables measure the same way;
        measured rather than written down, since the stylesheet decides.
        """
        combo.ensurePolished()
        if self._editor_height is None:
            probe = QLineEdit()
            probe.setFont(theme.mono_font(9))
            probe.ensurePolished()
            self._editor_height = probe.minimumSizeHint().height()
            probe.deleteLater()
        tallest = max(combo.minimumSizeHint().height(), self._editor_height)
        self.steps.verticalHeader().setDefaultSectionSize(
            tallest + 2 * theme.CELL_INSET_V)

    def _set_read_only(self, read_only):
        for widget in (self.name_edit, self.tags_edit):
            widget.setReadOnly(read_only)
        self.yaml.setReadOnly(read_only)
        self.steps.setEditTriggers(QTableWidget.NoEditTriggers if read_only
                                   else QTableWidget.AllEditTriggers)
        for button in (self.add_step_button, self.remove_step_button,
                       self.up_button, self.down_button):
            button.setEnabled(not read_only)
        for index in range(self.steps.rowCount()):
            widget = self.steps.cellWidget(index, 0)
            if widget is not None:
                widget.setEnabled(not read_only)
        self._update_buttons()

    # -- the document ---------------------------------------------------------
    def meta(self):
        tags = [tag.strip() for tag in self.tags_edit.text().split(",") if tag.strip()]
        return {"id": self.current["id"] if self.current else "",
                "name": self.name_edit.text().strip(),
                "description": (self.current or {}).get("meta", {}).get("description", ""),
                "tags": tags}

    def step_rows(self):
        """The Steps table as the flat dicts the core writes from."""
        rows = []
        for index in range(self.steps.rowCount()):
            combo = self.steps.cellWidget(index, 0)
            action = combo.currentText() if combo is not None else ""
            if not action:
                continue
            step = {"action": action}
            for key, column in (("target", 1), ("value", 2)):
                item = self.steps.item(index, column)
                text = item.text().strip() if item is not None else ""
                if text:
                    step[key] = text
            item = self.steps.item(index, 3)
            timeout = (item.text().strip() if item is not None else "")
            if timeout.isdigit():
                step["timeout"] = int(timeout)
            rows.append(step)
        return rows

    def document(self):
        """What Save sends: the text if that is what was edited, else the steps."""
        if self._last_edited == "yaml":
            return {"yaml": self.yaml.toPlainText()}
        return {"meta": self.meta(), "steps": self.step_rows()}

    # -- editing --------------------------------------------------------------
    def add_step(self):
        index = self.steps.rowCount()
        self.steps.insertRow(index)
        self._building = True
        try:
            self._fill_step_row(index, {"action": "click"})
        finally:
            self._building = False
        self.steps.selectRow(index)
        self._changed()

    def remove_step(self):
        index = self.steps.currentRow()
        if index >= 0:
            self.steps.removeRow(index)
            self._changed()

    def move_step(self, delta):
        index = self.steps.currentRow()
        target = index + delta
        if index < 0 or not 0 <= target < self.steps.rowCount():
            return
        rows = self.step_rows()
        rows[index], rows[target] = rows[target], rows[index]
        self._building = True
        try:
            self._load_steps(rows)
        finally:
            self._building = False
        self.steps.selectRow(target)
        self._changed()

    def open_target(self):
        """Follow the current step's target to whatever it is an alias for.

        A ``use:`` target is another flow, so it opens in this editor. Anything
        else is a selector name, so the Selectors tab is where the answer is -
        both are aliases, and reading one without being able to reach what it
        stands for is most of what makes a flow tree hard to learn.
        """
        index = self.steps.currentRow()
        if index < 0:
            return
        combo = self.steps.cellWidget(index, 0)
        action = combo.currentText() if combo is not None else ""
        item = self.steps.item(index, 1)
        target = item.text().strip() if item is not None else ""
        if not target:
            return
        if action in self.actions_for("use"):
            if not self._confirm_discard():
                return
            self.left.setCurrentIndex(0)
            if not self.select(target):
                self.open(target)     # a block the inventory has not listed
            return
        if target in self.inventory.selectors:
            self.left.setCurrentIndex(1)
            self.selector_search.setText(target)
            for row in range(self.selector_table.rowCount()):
                cell = self.selector_table.item(row, 0)
                if cell is not None and cell.text() == target:
                    self.selector_table.selectRow(row)
                    break
            return
        widgets.note(
            self, "Open target",
            "\"%s\" is not a name in selectors.yaml, so it is used as a raw "
            "selector exactly as written." % target)

    def revert(self):
        """Throw the edits away and re-read the file.

        No confirmation: the button is named Revert, it is only enabled while
        there is something to revert, and asking "discard your changes?" after
        someone has clicked "discard my changes" is a dialog that teaches people
        to dismiss dialogs.
        """
        if self.current:
            self.open(self.current["id"])

    def _step_edited(self, *_args):
        if self._building:
            return
        self._refresh_resolved()
        self._changed()

    def _yaml_edited(self):
        if self._building:
            return
        self._last_edited = "yaml"
        self._update_dirty()

    def _changed(self, *_args):
        if self._building:
            return
        self._last_edited = "steps"
        self._update_dirty()

    # -- unsaved changes ------------------------------------------------------
    # Same rule as Launch Sessions: the controls are a working copy of a file,
    # nothing writes back on its own, and switching away would drop the edit - so
    # Save goes red the moment the two have parted and quiet again when they meet.
    def _comparable(self):
        return json.dumps({"yaml": self.yaml.toPlainText(),
                           "meta": self.meta(), "steps": self.step_rows()},
                          sort_keys=True, default=str)

    def is_dirty(self):
        return self._dirty

    def _mark_clean(self):
        self._baseline = self._comparable()
        self._update_dirty()

    def _update_dirty(self):
        dirty = self._baseline is not None and self._comparable() != self._baseline
        self._dirty = dirty
        self.save_button.setProperty("dirty", "true" if dirty else "false")
        self.save_button.setToolTip("Unsaved changes" if dirty else "")
        # A stylesheet rule keyed on a property is only re-read on re-polish.
        self.save_button.style().unpolish(self.save_button)
        self.save_button.style().polish(self.save_button)
        self._update_buttons()

    def _confirm_discard(self):
        if not self._dirty or self.current is None:
            return True
        return widgets.confirm(
            self, "Unsaved changes",
            "Discard the changes to \"%s\"?"
            % (self.current["id"] if self.current else ""),
            "They have not been saved.", agree="Discard", kind="warn")

    def _update_buttons(self):
        writable = bool(self.current and self.current.get("writable"))
        has = self.current is not None
        self.save_button.setEnabled(writable)
        self.delete_button.setEnabled(writable)
        self.duplicate_button.setEnabled(has)
        self.export_button.setEnabled(has)
        self.revert_button.setEnabled(has and self._dirty)

    def _show_problems(self, payload):
        problems = list(payload.get("problems") or [])
        unresolved = payload.get("unresolved") or {}
        for flow_id in unresolved.get("use") or []:
            problems.append("uses %s, which does not exist here" % flow_id)
        notes = []
        for name in unresolved.get("selectors") or []:
            notes.append("%s is not in selectors.yaml (treated as raw CSS)" % name)
        if problems:
            self.problems.setText("\n".join(problems))
            self.problems.setStyleSheet("color: %s;" % theme.BAD)
        elif notes:
            self.problems.setText("\n".join(notes))
            self.problems.setStyleSheet("color: %s;" % theme.WARN)
        else:
            self.problems.setText("")

    # -- commands -------------------------------------------------------------
    def new_scenario(self):
        if not self._confirm_discard():
            return
        flow_id = self._ask_for_id("New scenario", "")
        if not flow_id:
            return
        # `template` keeps a half-written scenario out of --run-tests=all until
        # whoever is writing it takes the tag off.
        self._write(flow_id, {"meta": {"id": flow_id, "name": "", "tags": ["template"]},
                              "steps": [{"action": "use", "target": "auth.login"}]})

    def duplicate_scenario(self):
        if not self.current:
            return
        flow_id = self._ask_for_id("Duplicate scenario",
                                   self.current["id"] + "_copy")
        if not flow_id:
            return
        document = {"yaml": self.yaml.toPlainText()}
        self._write(flow_id, document)

    def _ask_for_id(self, title, initial):
        flow_id, ok = QInputDialog.getText(
            self, title, "Scenario id (letters, digits, _ and - only):", text=initial)
        return flow_id.strip() if ok else ""

    def _write(self, flow_id, document):
        if not self.core:
            return
        try:
            result = self.core.flow_save(flow_id, document)
        except core_mod.CoreError as exc:
            widgets.warn(self, "Scenarios", str(exc))
            return
        if not result.get("ok"):
            widgets.warn(self, "Scenarios",
                                "\n".join(result.get("problems")
                                          or ["could not write the scenario"]))
            return
        self.status.setText("wrote %s" % result.get("path", ""))
        # Settle before anything can ask: what is on screen is now what is on
        # disk, and re-selecting the row must not offer to discard it.
        self.current = None
        self._baseline = None
        self._update_dirty()
        self.saved.emit()   # the window re-reads --describe and repopulates the list
        # Re-open from disk rather than trusting the editor: the core may have
        # sanitised the id, and the YAML view should show the file as written.
        saved_id = result.get("id") or flow_id
        if not self.select(saved_id):
            self.open(saved_id)

    def save(self):
        if not self.current or not self.current.get("writable"):
            return
        self._write(self.current["id"], self.document())

    def delete_scenario(self):
        if not self.current or not self.current.get("writable"):
            return
        flow_id = self.current["id"]
        if not widgets.confirm(
                self, "Delete scenario", "Delete \"%s\"?" % flow_id,
                "The file is removed.", agree="Delete", kind="warn"):
            return
        try:
            result = self.core.flow_delete(flow_id)
        except core_mod.CoreError as exc:
            widgets.warn(self, "Scenarios", str(exc))
            return
        if not result.get("ok"):
            widgets.warn(self, "Scenarios", "\n".join(result["problems"]))
            return
        self.current = None
        self._baseline = None
        self._dirty = False
        self.status.setText("deleted %s" % flow_id)
        self.saved.emit()

    def import_scenario(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import a scenario", os.path.expanduser("~"),
            "Scenarios (*.yaml *.yml);;All files (*)")
        if not path:
            return
        try:
            result = self.core.flow_import(path)
        except core_mod.CoreError as exc:
            widgets.warn(self, "Scenarios", str(exc))
            return
        if not result.get("ok"):
            widgets.warn(self, "Import failed",
                                "\n".join(result.get("problems") or []))
            return
        self.status.setText("imported %s" % result.get("id", ""))
        self.saved.emit()

    def export_scenario(self):
        if not self.current:
            return
        suggested = os.path.join(os.path.expanduser("~"),
                                 "%s.yaml" % self.current["id"])
        path, _ = QFileDialog.getSaveFileName(self, "Export scenario", suggested,
                                              "Scenarios (*.yaml);;All files (*)")
        if not path:
            return
        if not self.write_to(path):
            widgets.warn(self, "Export failed", "Could not write %s." % path)
            return
        self.status.setText("exported to %s" % path)
        unresolved = self.current.get("unresolved") or {}
        # A scenario is one file, but its `use:` blocks and named selectors are
        # not - say so now rather than letting it fail wherever it lands.
        depends = list(unresolved.get("use") or []) + list(unresolved.get("selectors") or [])
        if depends:
            widgets.note(
                self, "Exported",
                "%s was written.\n\nIt refers to things this tree does not have, "
                "so wherever it goes needs them too:\n\n- %s"
                % (os.path.basename(path), "\n- ".join(depends)))

    # -- recording ------------------------------------------------------------
    # A recording happens in a Chrome window, not here, so this page's job is to
    # say what is being captured as it happens and to open the result when it is
    # written. The events arrive on the same --events stream everything else uses.
    def handle_recorder_event(self, event):
        kind = event.get("kind", "")
        if kind == "recorder.ready":
            self._recorded = []
            self.status.setText(
                "Recorder ready · press Capture Step (or F2) in a window")
        elif kind == "recorder.started":
            self._recorded = []
            self.status.setText("Recording %s …" % event.get("scenario", ""))
        elif kind == "recorder.step_captured":
            self._recorded.append(event)
            self.status.setText(
                "Recording %s · %d steps · last: %s %s"
                % (event.get("scenario", ""), len(self._recorded),
                   event.get("action", ""), event.get("target") or ""))
        elif kind == "recorder.step_failed":
            self.status.setText(
                "%s %s did not take effect: %s" % (event.get("action", ""),
                                                   event.get("target") or "",
                                                   event.get("error", "")))
        elif kind == "recorder.finished":
            scenario = event.get("scenario", "")
            if event.get("ok"):
                self.status.setText("Recorded %s · %d steps · %s"
                                    % (scenario, event.get("steps", 0),
                                       event.get("path", "")))
                # It is a real scenario now, so the list has to know about it -
                # and the obvious next thing is to look at what was captured.
                self.saved.emit()
                self._open_when_listed = scenario
                self.select(scenario)
            else:
                self.status.setText("Recording %s could not be written: %s"
                                    % (scenario, "; ".join(event.get("problems") or [])))

    # -- selectors ------------------------------------------------------------
    def load_selectors(self):
        """Read the user's selectors.yaml into the editor."""
        if not self.core:
            return
        try:
            payload = self.core.selectors_show()
        except core_mod.CoreError as exc:
            widgets.warn(self, "Selectors", str(exc))
            return
        self._building = True
        try:
            self.selector_yaml.setPlainText(payload.get("yaml") or "")
            self.selector_path.setText(payload.get("path", ""))
            writable = payload.get("writable", False)
            self.selector_yaml.setReadOnly(not writable)
            self.selector_save.setEnabled(writable)
            self.selector_tag.set("editable" if writable else "read-only",
                                  "accent" if writable else "neutral")
        finally:
            self._building = False
        self.selector_problems.setText("")
        self._reload_selector_list()

    def _selector_yaml_edited(self):
        if self._building:
            return
        self.selector_problems.setText("")

    def save_selectors(self):
        if not self.core:
            return
        try:
            result = self.core.selectors_save(self.selector_yaml.toPlainText())
        except core_mod.CoreError as exc:
            widgets.warn(self, "Selectors", str(exc))
            return
        if not result.get("ok"):
            self.selector_problems.setText("\n".join(result.get("problems") or []))
            self.selector_problems.setStyleSheet("color: %s;" % theme.BAD)
            return
        self.status.setText("wrote %s" % result.get("path", ""))
        # Every flow points at these names, so what changed here changes what
        # runs - the whole inventory has to be re-read.
        self.saved.emit()

    def override_selector(self):
        """Copy the selected name into the user's file, ready to be changed."""
        items = self.selector_table.selectedItems()
        if not items:
            return
        row = items[0].row()
        name = self.selector_table.item(row, 0).text()
        value = self.selector_table.item(row, 1).text()
        if name in self._selector_names(self.selector_yaml.toPlainText()):
            self.selector_problems.setText("%s is already one of yours." % name)
            self.selector_problems.setStyleSheet("color: %s;" % theme.WARN)
            return
        text = self.selector_yaml.toPlainText()
        if text and not text.endswith("\n"):
            text += "\n"
        self.selector_yaml.setPlainText(
            '%s%s: "%s"\n' % (text, name, value.replace('"', '\\"')))
        self.left.setCurrentIndex(1)

    def write_to(self, path):
        """Write the scenario being edited to ``path``; True when it lands.

        Split from the dialog so exporting can be tested without one.
        """
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.yaml.toPlainText())
        except OSError:
            return False
        return True
