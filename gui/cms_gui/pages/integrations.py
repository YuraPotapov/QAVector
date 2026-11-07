"""Integrations: the services this project is connected to.

The page opens on the provider it was left on - Jira, the only one so far - and
its breadcrumb leads back to the list of providers. Everything Jira is in
``jira.py``; this page hosts providers and coordinates their window lifecycle.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QHeaderView, QPushButton,
                               QStackedWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .. import search, widgets
from ..integrationhost import IntegrationHost
from .jira import JiraView

#: Provider id -> what the list calls it and says about it.
PROVIDERS = (("jira", "Jira", "Issues: read, search, and change them; cycles name a "
                              "connection instead of carrying a site and a token."),)


class IntegrationsPage(QWidget):
    """The rail's Integrations entry: the providers, and the one opened."""

    HEADERS = ("Provider", "Connections", "What it does")
    return_to_main_requested = Signal()
    #: What Search may ask this page to search changed: a connection was
    #: added, lost its token, or was read for the first time.
    search_sources_changed = Signal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.core = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.window_button = QPushButton("Open in separate window")
        self.window_button.clicked.connect(self.detach_selected)
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
        column.addWidget(widgets.row(self.open_button, self.window_button, None))
        self.stack.addWidget(catalog)
        self.catalog = catalog

        self.jira = JiraView(settings)
        self.jira.back_requested.connect(self._back_to_catalog)
        self.jira.connections_changed.connect(self._fill)
        self.jira.connections_changed.connect(self.search_sources_changed)
        # All providers are registered through a host: separate-window support
        # is part of the integration contract, not a Jira-only feature.
        self.views = {"jira": self.jira}
        self.hosts = {provider: IntegrationHost(provider, name, self.views[provider],
                                              self.stack, settings, self)
                      for provider, name, _description in PROVIDERS}
        for provider, host in self.hosts.items():
            host.docked.connect(lambda provider=provider: self._returned(provider))
        QApplication.instance().aboutToQuit.connect(self.shutdown)
        self._fill()
        self._restore()

    # -- the outside ----------------------------------------------------------
    def set_core(self, core):
        changed = core is not self.core
        self.core = core
        for view in self.views.values():
            view.set_core(core)
        if changed:
            self._restore()
            self._fill()

    def set_project_name(self, name):
        for host in self.hosts.values():
            host.set_project_name(name)

    def _returned(self, provider):
        self._save_view(provider)
        self.return_to_main_requested.emit()

    def has_unsaved(self):
        return any(view.has_unsaved() for view in self.views.values())

    def busy_reason(self, include_dialogs=True):
        return next((reason for view in self.views.values()
                     if (reason := view.busy_reason(include_dialogs))), "")

    def shutdown(self):
        for view in self.views.values():
            view.shutdown()
        for host in self.hosts.values():
            host.shutdown()

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)

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
    # -- search (Ctrl+K) --------------------------------------------------------
    # Every provider answers for itself, and the page files what it answers
    # under the provider's id: a target here is ``(provider, its own target)``.
    # A provider's view offers ``search_rows()`` - what it has loaded, never
    # asking its service - and ``search_source()`` with ``search_remote(query,
    # done)``: its service, searched only when that row of Search is chosen.
    # Results are :class:`search.Hit`s and nothing else.
    def search_rows(self):
        """What the providers have loaded. Never asks a service: what has not
        been loaded is not offered."""
        found = []
        for provider, view in self.views.items():
            rows = getattr(view, "search_rows", None)
            if rows is not None:
                found.extend(one._replace(target=(provider, one.target)) for one in rows()
                             if isinstance(one, search.Hit))
        return found

    def search_sources(self):
        """A source per provider that can be searched now - for Jira, a
        connection with a token. With none set up, none is offered."""
        found = []
        for provider, view in self.views.items():
            offered = getattr(view, "search_source", None)
            source = offered() if offered is not None else None
            if source:
                title, detail = source
                found.append(search.Source("", provider, title, detail))
        return found

    def search_source(self, provider, query, done):
        """Search one provider's service, because its row was chosen."""
        view = self.views.get(provider)
        if view is None or getattr(view, "search_remote", None) is None:
            done([], "%s cannot be searched." % provider)
            return

        def answered(hits, error=None):
            done([one._replace(target=(provider, one.target)) for one in hits or []
                  if isinstance(one, search.Hit)], error)

        view.search_remote(query, answered)

    def show_found(self, target):
        if not (isinstance(target, tuple) and len(target) == 2 and target[0] in self.views):
            return False
        provider, inner = target
        shown = self.views[provider].show_found(inner)
        if shown:
            self.open_provider(provider)
        return shown

    # -- which view -----------------------------------------------------------
    def show_catalog(self):
        self.stack.setCurrentWidget(self.catalog)
        self._save_view("")

    def _back_to_catalog(self):
        self.show_catalog()
        self.return_to_main_requested.emit()

    def open_provider(self, provider):
        if provider in self.hosts:
            self.hosts[provider].open()
            self._save_view(provider)

    def detach_provider(self, provider):
        if provider in self.hosts:
            self._save_view(provider)
            return self.hosts[provider].detach()

    def detach_selected(self):
        current = self.stack.currentWidget()
        for provider, host in self.hosts.items():
            if current in (host.view, host.placeholder):
                return self.detach_provider(provider)
        provider = self.selected_provider()
        if provider:
            return self.detach_provider(provider)

    def open_selected(self):
        provider = self.selected_provider()
        if provider:
            self.open_provider(provider)

    def selected_provider(self):
        rows = self.table.selectionModel().selectedRows()
        item = self.table.item(rows[0].row() if rows else 0, 0)
        return item.data(Qt.UserRole) if item is not None else ""

    def _restore(self):
        view = self.settings.integrations_state().get("view", "jira")
        if view in self.hosts:
            host = self.hosts[view]
            self.stack.setCurrentWidget(host.placeholder if host.window else host.view)
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
