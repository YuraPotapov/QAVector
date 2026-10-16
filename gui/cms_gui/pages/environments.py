"""Environments page: what ``--env`` can select, and the per-run directories.

Environments are not a thing the launcher stores anywhere - they are *derived*
from the distinct ``env`` values in users.json (session_launcher.build_environments),
which is why this page reads them from ``--describe`` and edits them through the
config file rather than through some GUI-only registry.

The URL override and the default directories genuinely have no home in the core,
so those live in QSettings and are handed to the Command page as defaults.
"""

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from .. import icons, search, theme, widgets


class EnvironmentsPage(QWidget):
    """Read-only inventory of environments + the GUI's own per-env extras."""

    directories_changed = Signal()

    HEADERS = ["Alias", "env value (users.json)", "Origin", "Users", "URL override"]

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._envs = []

        column = QVBoxLayout(self)
        column.setContentsMargins(24, 20, 24, 20)
        column.setSpacing(6)
        self.wording = widgets.Phrasing()
        column.addWidget(widgets.heading("Environments"))
        column.addWidget(self.wording.text(
            widgets.lede(""),
            "Read from the launcher. The env value and the user rows live in "
            "users.json; URL overrides and default directories are stored by the GUI.",
            "Read from --describe. The env value and the user rows live in users.json; "
            "URL overrides and default directories are stored by the GUI."))
        column.addSpacing(12)

        panel = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        header = self.table.horizontalHeader()
        for index in range(len(self.HEADERS)):
            header.setSectionResizeMode(index, QHeaderView.Interactive)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setMinimumHeight(140)
        panel.layout().addWidget(self.table)
        column.addWidget(panel)

        column.addSpacing(18)
        column.addWidget(widgets.kicker("Default directories"))
        column.addWidget(self.wording.text(
            widgets.lede(""),
            "Where every run reads and writes. Empty means the launcher's own default.",
            "Passed to every run as --flows-dir / --reports-dir / --sessions-dir. "
            "Empty means the core's own default."))
        column.addSpacing(6)

        self.dir_edits = {}
        dirs = QHBoxLayout()
        dirs.setSpacing(16)
        for key, plain, flag in (("flows", "Flows", "--flows-dir"),
                                 ("reports", "Reports", "--reports-dir"),
                                 ("sessions", "Sessions", "--sessions-dir")):
            edit = QLineEdit(self.settings.directory(key))
            edit.setProperty("mono", True)
            edit.setPlaceholderText("(default)")
            edit.editingFinished.connect(
                lambda k=key, e=edit: self._save_directory(k, e.text()))
            browse = icons.button(QPushButton(""), "browse")
            browse.setFixedWidth(38)
            browse.clicked.connect(lambda _c=False, k=key, e=edit: self._browse(k, e))
            self.dir_edits[key] = edit
            box = widgets.field(plain, widgets.row(edit, browse, stretch_last=False))
            self.wording.text(box.label, plain, flag)
            dirs.addWidget(box, 1)
        column.addLayout(dirs)
        column.addStretch(1)

    def set_developer_mode(self, enabled):
        self.wording.apply(enabled)

    # -- data -----------------------------------------------------------------
    def set_inventory(self, inventory):
        self._envs = inventory.envs
        self.table.setRowCount(len(self._envs))
        for row, env in enumerate(self._envs):
            alias = env.get("alias", "")
            self._set(row, 0, alias, mono=True)
            self._set(row, 1, env.get("value", ""), mono=True)
            self._set(row, 2, env.get("origin", "") or "(no URL)", mono=True,
                      color=theme.ACCENT_RAMP[700] if env.get("origin")
                      else theme.NEUTRAL[600])
            self._set(row, 3, str(env.get("count", "")))
            override = QLineEdit(self.settings.env_override(alias))
            override.setProperty("mono", True)
            override.setPlaceholderText("inherit")
            override.editingFinished.connect(
                lambda a=alias, e=override: self.settings.set_env_override(a, e.text()))
            self.table.setCellWidget(row, 4, override)
        self._fit_rows_to_the_override()
        self.table.resizeColumnsToContents()
        if self.table.columnWidth(4) < 240:
            self.table.setColumnWidth(4, 240)
        # Directory defaults follow the core unless the user set their own.
        for key, value in inventory.dirs().items():
            edit = self.dir_edits.get(key)
            if edit is not None and not edit.text():
                edit.setPlaceholderText(value or "(default)")

    def _fit_rows_to_the_override(self):
        """Make a row tall enough for the editor it holds.

        Every other column is text, which a row of any height can draw. The URL
        override is a real QLineEdit, and the design's inputs are 30px - taller
        than a row of text - while the view insets a cell widget by the item
        padding on both sides. In a row sized for text the editor was handed
        24px, rendered at its own 30 regardless, and the extra hung *downwards*:
        3px of air above it and 3px of it lying across the gridline below, which
        is the lopsidedness you could see rather than measure.

        Measured, not written down: how tall an input comes out is the body
        font's doing and the stylesheet's, and neither is fixed here.
        """
        tallest = 0
        for row in range(self.table.rowCount()):
            editor = self.table.cellWidget(row, 4)
            if editor is None:
                continue
            editor.ensurePolished()
            tallest = max(tallest, editor.minimumSizeHint().height())
        if tallest:
            self.table.verticalHeader().setDefaultSectionSize(
                tallest + 2 * theme.CELL_INSET_V)

    def _set(self, row, col, text, mono=False, color=None):
        item = QTableWidgetItem(text)
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        if mono:
            item.setFont(theme.mono_font(9))
        if color:
            item.setForeground(theme.color(color))
        self.table.setItem(row, col, item)

    def override_for(self, alias):
        return self.settings.env_override(alias)

    def directories(self):
        return {key: edit.text().strip() for key, edit in self.dir_edits.items()}

    # -- search (Ctrl+K) -------------------------------------------------------
    def search_rows(self):
        """Every environment: its name, where it is, how many accounts it has."""
        return [search.hit(
            env.get("alias", ""),
            " · ".join(part for part in (env.get("origin") or "",
                                         "%s accounts" % env.get("count", 0)) if part),
            keys=env.get("value", ""), target=env.get("alias", ""))
            for env in self._envs]

    def show_found(self, alias):
        for row, env in enumerate(self._envs):
            if env.get("alias") == alias:
                self.table.selectRow(row)
                self.table.scrollToItem(self.table.item(row, 0))
                return True
        return False

    # -- editing --------------------------------------------------------------
    def _save_directory(self, key, value):
        self.settings.set_directory(key, value.strip())
        self.directories_changed.emit()

    def _browse(self, key, edit):
        start = edit.text() or os.path.expanduser("~")
        chosen = QFileDialog.getExistingDirectory(self, "Choose a directory", start)
        if chosen:
            edit.setText(chosen)
            self._save_directory(key, chosen)
