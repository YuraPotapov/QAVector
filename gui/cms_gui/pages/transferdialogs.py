"""The two windows of File -> Export and File -> Import.

**Export** is one tree of everything that can go in a file, ticked: projects
and their environments, users, cycle projects and their cycles. Ticking a
project ticks its environments; what an environment brings - its users - comes
along without being ticked twice. Opened from a page, the thing that page was
showing is ticked already.

**Import** says what the file holds and which of it is already here, and - when
something is - asks once whether to add to the existing ones or to add copies.
What either means is :mod:`cms_gui.transfer`'s business.
"""

import datetime
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QDialog, QHBoxLayout,
                               QHeaderView, QLabel, QPushButton, QRadioButton,
                               QTableWidgetItem, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout)

from .. import launchprojectsfile as lpf, theme, transfer, usersfile, widgets

#: What a tree item stands for, kept on it.
KIND = Qt.UserRole + 1
VALUE = Qt.UserRole + 2

NO_PROJECT = "(no project)"


def _buttons(dialog, agree, slot):
    row = QHBoxLayout()
    row.addStretch(1)
    cancel = QPushButton("Cancel")
    cancel.clicked.connect(dialog.reject)
    row.addWidget(cancel)
    button = QPushButton(agree)
    button.setProperty("variant", "primary")
    button.setDefault(True)
    button.clicked.connect(slot)
    row.addWidget(button)
    return row, button


class ExportDialog(QDialog):
    """Tick what goes in the file; :meth:`selection` says what was ticked."""

    def __init__(self, parent, projects, catalog, rows, cycle_rows, chosen=None,
                 secrets_included=False, project_name=""):
        super().__init__(parent)
        #: What the project on screen is called - its environments' group.
        self.project_name = project_name
        self.setWindowTitle("Export")
        self.projects, self.catalog, self.rows = projects, catalog, rows
        self.cycle_rows = [row for row in cycle_rows if row.get("id")]
        chosen = chosen or {}
        self.frame = widgets.dress(self)
        column = QVBoxLayout(self)
        column.setSpacing(8)
        column.addWidget(widgets.lede(
            "Tick what goes in the file. A project brings its environments, an "
            "environment its users, a cycle project its cycles."))

        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderHidden(True)
        self.tree.setSelectionMode(QAbstractItemView.NoSelection)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tree.header().setStretchLastSection(True)
        self.tree.setMinimumHeight(340)
        self._fill(chosen)
        self.tree.itemChanged.connect(lambda *_a: self._summarise())
        column.addWidget(self.tree, 1)

        self.summary = widgets.mono("")
        column.addWidget(self.summary)
        note = QLabel("Passwords%s are written in the file as they are - keep it as "
                      "private as users.json."
                      % (" and cycle secrets" if secrets_included else ""))
        note.setProperty("role", "hint")
        note.setWordWrap(True)
        column.addWidget(note)
        if not secrets_included:
            self.secrets_note = QLabel("Cycle secret values are not in the file yet: "
                                       "set them again after importing.")
            self.secrets_note.setProperty("role", "hint")
            self.secrets_note.setWordWrap(True)
            column.addWidget(self.secrets_note)
        buttons, self.export_button = _buttons(self, "Export…", self.accept)
        column.addLayout(buttons)
        self.resize(max(620, self.sizeHint().width()), max(560, self.sizeHint().height()))
        self._summarise()

    # -- the tree -------------------------------------------------------------
    def _group(self, title):
        item = QTreeWidgetItem(self.tree, [title, ""])
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate)
        item.setCheckState(0, Qt.Unchecked)
        item.setFont(0, theme.mono_font(9))
        return item

    def _item(self, parent, kind, value, label, detail, checked, tristate=False):
        item = QTreeWidgetItem(parent, [label, detail])
        flags = Qt.ItemIsEnabled | Qt.ItemIsUserCheckable
        if tristate:
            flags |= Qt.ItemIsAutoTristate
        item.setFlags(flags)
        item.setData(0, KIND, kind)
        item.setData(0, VALUE, value)
        item.setForeground(1, theme.color(theme.NEUTRAL[600]))
        item.setCheckState(0, Qt.Checked if checked else Qt.Unchecked)
        return item

    def _fill(self, chosen):
        names = dict(lpf.project_names(self.projects, self.catalog))
        if self.project_name and lpf.WORKSPACE in names:
            names[lpf.WORKSPACE] = self.project_name
        envs = self.tree.blockSignals(True)
        launch = self._group("Projects and environments")
        counts = {}
        for row in self.rows:
            counts[row.env] = counts.get(row.env, 0) + 1
        for project_id, name in names.items():
            mine = [env for env in self.catalog if env.project_id == project_id]
            whole = project_id in set(chosen.get("projects") or ())
            project = self._item(launch, "project", project_id, name,
                                 "%d environment(s)" % len(mine), whole, tristate=True)
            for env in mine:
                users = counts.get(env.account_key, 0)
                self._item(project, "env", env.id, env.name,
                           "%d user(s)" % users if users else "no users",
                           whole or env.id in set(chosen.get("environments") or ()))
            if not mine:
                project.setFlags(project.flags() & ~Qt.ItemIsAutoTristate)
        people = self._group("Users")
        env_names = {}
        for env in self.catalog:
            env_names.setdefault(env.account_key, env.name)
        picked = set(chosen.get("users") or ())
        for person in usersfile.credentials(self.rows):
            if not person.login:
                continue
            places = [env_names.get(env, env) for env in person.envs(self.rows)]
            self._item(people, "user", person.key, person.login,
                       " · ".join(part for part in (person.role, ", ".join(places))
                                  if part),
                       person.key in picked)
        cycles = self._group("Cycles")
        by_project = {}
        for row in self.cycle_rows:
            by_project.setdefault(row.get("project") or "", []).append(row)
        whole_cycles = set(chosen.get("cycle_projects") or ())
        picked_cycles = set(chosen.get("cycles") or ())
        for name in sorted(by_project, key=lambda one: (one == "", one.casefold())):
            project = self._item(cycles, "cycle_project", name, name or NO_PROJECT,
                                 "%d cycle(s)" % len(by_project[name]),
                                 name in whole_cycles, tristate=True)
            for row in by_project[name]:
                self._item(project, "cycle", row["id"], row["id"], row.get("name") or "",
                           name in whole_cycles or row["id"] in picked_cycles)
        for index in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(index)
            self._expand_ticked(group)
        self.tree.blockSignals(envs)

    def _expand_ticked(self, item):
        """Open the branches something is ticked in, so it is seen."""
        ticked = item.checkState(0) != Qt.Unchecked
        for index in range(item.childCount()):
            ticked = self._expand_ticked(item.child(index)) or ticked
        if item.childCount():
            item.setExpanded(ticked)
        return ticked

    def _walk(self):
        stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            item = stack.pop(0)
            yield item
            stack.extend(item.child(i) for i in range(item.childCount()))

    # -- what was ticked ------------------------------------------------------
    def selection(self):
        """``{projects, environments, users, cycle_projects, cycles}`` ticked.

        A project ticked whole is the project (so an empty one goes too); one
        ticked in part is the environments ticked in it. The same for cycles.
        """
        found = {"projects": [], "environments": [], "users": [],
                 "cycle_projects": [], "cycles": []}
        for item in self._walk():
            kind, value = item.data(0, KIND), item.data(0, VALUE)
            state = item.checkState(0)
            if kind == "project" and state == Qt.Checked:
                found["projects"].append(value)
            elif kind == "env" and state == Qt.Checked \
                    and item.parent().checkState(0) != Qt.Checked:
                found["environments"].append(value)
            elif kind == "user" and state == Qt.Checked:
                found["users"].append(tuple(value))
            elif kind == "cycle_project" and state == Qt.Checked:
                found["cycle_projects"].append(value)
            elif kind == "cycle" and state == Qt.Checked \
                    and item.parent().checkState(0) != Qt.Checked:
                found["cycles"].append(value)
        return found

    def launch_document_part(self):
        chosen = self.selection()
        return transfer.launch_part(self.projects, self.catalog, self.rows,
                                    chosen["projects"], chosen["environments"],
                                    chosen["users"])

    def _summarise(self):
        chosen = self.selection()
        cycles = [row for row in self.cycle_rows
                  if row["id"] in chosen["cycles"]
                  or (row.get("project") or "") in chosen["cycle_projects"]]
        preview = transfer.bundle(self.launch_document_part(), {
            "cycle_projects": [{"name": name} for name in chosen["cycle_projects"]
                               if name],
            "cycles": [{"id": row["id"]} for row in cycles]})
        said = transfer.counts(preview)
        self.summary.setText("In the file: %s" % said)
        self.export_button.setEnabled(said != "nothing")

    def suggested_name(self):
        """A file name from what is in it: the one thing, or the date."""
        chosen = self.selection()
        single = [value for values in chosen.values() for value in values]
        stamp = datetime.date.today().isoformat()
        if len(single) == 1 and not chosen["users"]:
            label = next(item.text(0) for item in self._walk()
                         if item.data(0, VALUE) == single[0])
            base = "qavector-%s-%s" % ("".join(c if c.isalnum() or c in "-_" else "-"
                                               for c in label).strip("-") or "export",
                                       stamp)
        else:
            base = "qavector-export-%s" % stamp
        return base + transfer.SUFFIX


class ImportDialog(QDialog):
    """What a file holds, what of it is here already, and what to do about that."""

    HEADERS = ["What", "Name", "Here"]

    def __init__(self, parent, path, document, items):
        super().__init__(parent)
        self.setWindowTitle("Import")
        self.frame = widgets.dress(self)
        column = QVBoxLayout(self)
        column.setSpacing(8)
        column.addWidget(widgets.lede("%s holds %s."
                                      % (os.path.basename(path), transfer.counts(document))))
        panel = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        self.table = widgets.Table(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setRowCount(len(items))
        for row, item in enumerate(items):
            for col, text in enumerate((item.kind, item.name,
                                        "already here" if item.exists else "new")):
                cell = QTableWidgetItem(text)
                if col == 1:
                    cell.setFont(theme.mono_font(9))
                if col == 2:
                    cell.setForeground(theme.color(theme.ACCENT_RAMP[700] if item.exists
                                                   else theme.NEUTRAL[600]))
                self.table.setItem(row, col, cell)
        self.table.resizeColumnToContents(0)
        self.table.resizeColumnToContents(2)
        self.table.setMinimumHeight(220)
        widgets.empty_note(self.table, "The file holds nothing to import.")
        panel.layout().addWidget(self.table)
        column.addWidget(panel, 1)

        self.conflicts = [item for item in items if item.exists]
        self.merge = QRadioButton("Add to the existing ones")
        self.copy = QRadioButton("Add as copies")
        self.merge.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.merge)
        group.addButton(self.copy)
        if self.conflicts:
            column.addWidget(widgets.kicker("%d already here" % len(self.conflicts)))
            column.addWidget(self.merge)
            column.addWidget(self._hint(
                "An environment gets the variables and users it is missing - nothing "
                "of yours changes; a project its environments; a cycle the imported "
                "text."))
            column.addWidget(self.copy)
            column.addWidget(self._hint(
                "Beside what is here, named \"... (copy)\". A copied environment signs "
                "in with browser profiles of its own."))
        else:
            self.merge.setVisible(False)
            self.copy.setVisible(False)
        buttons, self.import_button = _buttons(self, "Import", self.accept)
        self.import_button.setEnabled(bool(items))
        column.addLayout(buttons)
        self.resize(max(600, self.sizeHint().width()), max(480, self.sizeHint().height()))

    @staticmethod
    def _hint(text):
        label = QLabel(text)
        label.setProperty("role", "hint")
        label.setWordWrap(True)
        label.setContentsMargins(24, 0, 0, 4)
        return label

    def mode(self):
        return transfer.COPY if self.copy.isChecked() else transfer.MERGE
