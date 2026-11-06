"""Integrations: the services this project is connected to.

The page opens on the provider it was left on - Jira, the only one so far - and
its breadcrumb leads back to the list of providers. Everything Jira is in
``jira.py``; this page only chooses which view is showing.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QHeaderView, QPushButton,
                               QStackedWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .. import widgets
from .jira import JiraView

#: Provider id -> what the list calls it and says about it.
PROVIDERS = (("jira", "Jira", "Issues: read, search, and change them; cycles name a "
                              "connection instead of carrying a site and a token."),)


class IntegrationsPage(QWidget):
    """The rail's Integrations entry: the providers, and the one opened."""

    HEADERS = ("Provider", "Connections", "What it does")

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack)

        catalog = QWidget()
        column = QVBoxLayout(catalog)
        column.setContentsMargins(24, 22, 24, 18)
        column.setSpacing(8)
        column.addWidget(widgets.heading("Integrations"))
        column.addWidget(widgets.lede(
            "Services this project is connected to. A connection is set once here, "
            "and cycles name it instead of carrying a site, an account and a token."))
        column.addSpacing(8)
        panel = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        self.table = widgets.Table(0, len(self.HEADERS), reclick_clears=False)
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.setSectionResizeMode(1, QHeaderView.Interactive)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 180)
        self.table.setColumnWidth(1, 140)
        self.table.itemDoubleClicked.connect(lambda _item: self.open_selected())
        self.search = widgets.SearchRow(self.table)
        self.search.changed.connect(lambda: self.note.say_filtered(
            "No provider matches the search." if self.search.searching() else ""))
        self.note = widgets.empty_note(self.table, "No providers in this build.")
        panel.layout().addWidget(self.table)
        column.addWidget(panel, 1)
        self.open_button = QPushButton("Open")
        self.open_button.setProperty("variant", "primary")
        self.open_button.clicked.connect(self.open_selected)
        column.addWidget(widgets.row(self.open_button, None))
        self.stack.addWidget(catalog)
        self.catalog = catalog

        self.jira = JiraView(settings)
        self.jira.back_requested.connect(self.show_catalog)
        self.jira.connections_changed.connect(self._fill)
        self.stack.addWidget(self.jira)
        self._fill()
        self._restore()

    # -- the outside ----------------------------------------------------------
    def set_core(self, core):
        changed = core is not self.jira.core
        self.jira.set_core(core)
        if changed:
            self._restore()
            self._fill()

    def set_developer_mode(self, enabled):
        """A developer can bring the project's own OAuth app and sign-in service;
        everybody else just signs in."""
        self.jira.connections_tab.developer = bool(enabled)

    def showEvent(self, event):
        super().showEvent(event)
        # The list counts connections: the project's file, read by the core.
        # Jira itself is not asked anything by showing this.
        if self.stack.currentWidget() is self.catalog and not self.jira._listed:
            self.jira.list_connections(then=self._fill)

    # -- Ctrl+K ---------------------------------------------------------------
    def search_rows(self):
        """The connections, and the issues already loaded. Never asks Jira:
        what has not been loaded is not offered."""
        return self.jira.search_rows()

    def show_found(self, target):
        shown = self.jira.show_found(target)
        if shown:
            self.stack.setCurrentWidget(self.jira)
            self._save_view("jira")
        return shown

    # -- which view -----------------------------------------------------------
    def show_catalog(self):
        self.stack.setCurrentWidget(self.catalog)
        self._save_view("")

    def open_provider(self, provider):
        if provider == "jira":
            self.stack.setCurrentWidget(self.jira)
            self._save_view("jira")

    def open_selected(self):
        rows = self.table.selectionModel().selectedRows()
        item = self.table.item(rows[0].row() if rows else 0, 0)
        if item is not None:
            self.open_provider(item.data(Qt.UserRole))

    def _restore(self):
        view = self.settings.integrations_state().get("view", "jira")
        if view == "jira":
            self.stack.setCurrentWidget(self.jira)
        else:
            self.stack.setCurrentWidget(self.catalog)

    def _save_view(self, view):
        state = dict(self.settings.integrations_state())
        state["view"] = view
        self.settings.save_integrations_state(state)

    def _fill(self):
        self.table.setRowCount(0)
        for provider, name, what in PROVIDERS:
            row = self.table.rowCount()
            self.table.insertRow(row)
            count = len(self.jira.connections) if provider == "jira" else 0
            listed = self.jira._listed if provider == "jira" else True
            values = (name, ("%d" % count) if listed else "—", what)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.UserRole, provider)
                self.table.setItem(row, column, item)
        self.search.apply()
