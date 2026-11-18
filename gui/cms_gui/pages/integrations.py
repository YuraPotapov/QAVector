"""Integrations: the services this project is connected to.

The page opens on the provider it was left on - Jira, unless another was - and
its breadcrumb leads back to the list of providers. Everything Jira is in
``jira.py`` and everything Agents in ``agents.py``; this page hosts providers
and coordinates their window lifecycle.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QScrollArea, QStackedWidget,
                               QVBoxLayout, QWidget)

from .. import search, widgets
from ..integrationhost import IntegrationHost
from .agents import AgentsView
from .agentusage import StatTile
from .jira import JiraView
from .providercards import Provider, ProviderGrid, standing_of
from .teams import TeamsView

#: Each provider as its card tells it: id, name, what kind of service it is,
#: what it does, its maker's mark (brandmarks.py), what can be done through it, and
#: optionally a (flag, why) the card wears beside its name.
PROVIDERS = (("jira", "Jira", "Issue tracker",
              "Issues: read, search, and change them; cycles name a connection instead of "
              "carrying a site and a token.", "jira",
              ("Read", "Search", "Change", "Agents", "Cycles")),
             ("teams", "Teams", "Team chat",
              "Posts to a channel or chat through its workflow; cycles and agents post through "
              "a channel set up here, one post a minute.", "teams",
              ("Post", "History", "Agents", "Cycles"),
              # Webhook posts only (ADR-015): said on the card, not found out later.
              ("Limited", "Posts only, through the channel's workflow webhook: it cannot read the "
                          "channel, edit or take back a post, and sends one post a minute.")),
             ("agents", "Agents", "Coding agents",
              "Agent programs such as Claude Code, set once for every project, and every turn "
              "taken with one.", "agents",
              ("Chat", "Delegate", "Budgets", "Usage")))


class IntegrationsPage(QWidget):
    """The rail's Integrations entry: the providers, and the one opened."""

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
        self.stack = QStackedWidget()
        outer.addWidget(self.stack)

        catalog = QWidget()
        column = QVBoxLayout(catalog)
        column.setContentsMargins(24, 22, 24, 18)
        column.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(24)
        head.addWidget(widgets.heading("Integrations"), 1, Qt.AlignVCenter)
        # The whole of it at a glance: what is there, and what waits for the person.
        self.tiles = {key: StatTile(label, hint) for key, label, hint in (
            ("providers", "Providers", "Services this build can connect to."),
            ("connections", "Connections", "Connections set up, across every provider."),
            ("waiting", "Waiting for you", "Changes agents asked for that wait for your Approve."))}
        tiles = QHBoxLayout()
        tiles.setSpacing(8)
        for tile in self.tiles.values():
            tile.setFixedWidth(152)
            tiles.addWidget(tile)
        head.addLayout(tiles)
        head.setAlignment(tiles, Qt.AlignTop)
        column.addLayout(head)
        column.addSpacing(12)
        self.cards = ProviderGrid([Provider(*one) for one in PROVIDERS])
        self.cards.open_requested.connect(self.open_provider)
        self.cards.detach_requested.connect(self.detach_provider)
        # The cards scroll: stacked one to a row on a narrow window they are
        # taller than a laptop's screen, and must not hold the window tall.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        body.setProperty("seethrough", True)
        cards = QVBoxLayout(body)
        cards.setContentsMargins(0, 0, 0, 0)
        cards.addWidget(self.cards)
        if not PROVIDERS:
            cards.addWidget(widgets.empty_zone(
                "No providers in this build", "This build of QAVector connects to no service."))
        cards.addStretch(1)
        scroll.setWidget(body)
        column.addWidget(scroll, 1)
        self.stack.addWidget(catalog)
        self.catalog = catalog

        self.jira = JiraView(settings)
        self.jira.back_requested.connect(self._back_to_catalog)
        self.jira.connections_changed.connect(self._fill)
        self.jira.connections_changed.connect(self.search_sources_changed)
        self.agents = AgentsView(settings)
        # A conversation's issue, opened from its header (ADR-014).
        self.agents.chat.issue_wanted.connect(self._issue_from_chat)
        self.agents.back_requested.connect(self._back_to_catalog)
        self.agents.connections_changed.connect(self._fill)
        self.teams = TeamsView(settings)
        self.teams.back_requested.connect(self._back_to_catalog)
        self.teams.connections_changed.connect(self._fill)
        # All providers are registered through a host: separate-window support
        # is part of the integration contract, not a Jira-only feature.
        self.views = {"jira": self.jira, "teams": self.teams, "agents": self.agents}
        self.hosts = {provider: IntegrationHost(provider, name, self.views[provider],
                                              self.stack, settings, self)
                      for provider, name, *_rest in PROVIDERS}
        for provider, host in self.hosts.items():
            host.docked.connect(lambda provider=provider: self._returned(provider))
        # A Jira issue handed to an agent: the one thing that knows both, added
        # to Jira's opened issue from here, where both views are. Jira knows
        # nothing of it, nor the agents of Jira.
        from ..delegation import Delegation
        self.delegation = Delegation(self.jira, self.agents, settings, self._show_conversation)
        self.jira.add_issue_extension(self.delegation)
        # What agents asked to change in Jira, waiting for the person (ADR-007):
        # added from here, so Jira knows nothing of agents.
        from .integrationchanges import IntegrationChangesTab
        self.changes = IntegrationChangesTab("jira")
        self.jira.add_tab(self.changes, self.changes.title())
        # The posts agents want to send, on Teams' own page (ADR-015 D6).
        self.teams_changes = IntegrationChangesTab("teams")
        self.teams.add_tab(self.teams_changes, self.teams_changes.title())
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
            # Another project: what the cards said was the last one's. Its own
            # are read now if the cards are on screen - a view reads again only
            # when it is itself shown, and on the catalog none of them is.
            for card in self.cards.cards.values():
                card.set_mark("")
            self._fill()
            if self.isVisible() and self.stack.currentWidget() is self.catalog:
                self._count_connections()

    def set_change_source(self, source):
        """QAVector's core, whose agents' changes to integrations the page lists."""
        self.changes.set_source(source)
        self.teams_changes.set_source(source)
        source.integration_changes_changed.connect(self._changes_changed)

    def _changes_changed(self):
        self.changes.refresh()
        self.jira.set_tab_title(self.changes, self.changes.title())
        self.teams_changes.refresh()
        self.teams.set_tab_title(self.teams_changes, self.teams_changes.title())
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
        if self.stack.currentWidget() is self.catalog:
            self._count_connections()

    def _count_connections(self):
        """Read what the list counts: each provider's connections, once. Files,
        read by the core - neither Jira nor an agent's program is asked
        anything. A provider that was opened has read its own already."""
        for view in self.views.values():
            if not view._listed:
                view.list_connections(then=self._fill)
        # Agents wears the mark of the program used last (Claude, Codex); both
        # before any conversation.
        card = self.cards.cards.get("agents")
        if card is not None:
            self.agents.last_kind(card.set_mark)

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

        def answered(hits, error=None, within=""):
            found = [one._replace(target=(provider, one.target)) for one in hits or []
                     if isinstance(one, search.Hit)]
            # ``within`` only when the search was bounded: a caller that knows
            # nothing of bounds is answered as it always was.
            done(found, error, within) if within else done(found, error)

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
        if self.isVisible():
            # Come back to from one provider: the other was never opened, and
            # a dash where its count goes would read as "cannot tell".
            self._count_connections()

    def _back_to_catalog(self):
        self.show_catalog()
        self.return_to_main_requested.emit()

    def _show_conversation(self, session_id=None):
        """The agents' chat in front - on a conversation, when one is named.
        It is reached from a Jira issue, and its breadcrumb leads back there."""
        self.open_provider("agents", self._issue_trail())
        if session_id:
            self.agents.open_session(session_id)

    def _issue_trail(self):
        """``Jira / KEY`` for the issue Delegation is on: the issue part opens
        it again, wherever Jira has gone since."""
        trail = [("Jira", lambda: self.open_provider("jira"))]
        key, connection_id = self.delegation.key(), self.delegation.connection_id
        if key:
            trail.append((key, lambda: self._back_to_issue(connection_id, key)))
        return trail

    def _issue_from_chat(self, connection_id, key):
        """An issue a conversation is about, opened from its ABOUT chip (ADR-014):
        Jira's breadcrumb leads back to that conversation, and the conversation's
        own, once there, to the issue again - the way taken, both ways."""
        session = self.agents.chat.current or {}
        session_id = session.get("id") or ""
        title = session.get("title") or "Chat"
        title = title if len(title) <= 40 else title[:39] + "..."

        def back():
            self.open_provider("agents", [("Jira", lambda: self.open_provider("jira")),
                                          (key, lambda: self._issue_from_chat(connection_id, key))])
            if session_id:
                self.agents.open_session(session_id)
        self.jira.open_key(connection_id, key)
        self.open_provider("jira", [("Agents", lambda: self.open_provider("agents")), (title, back)])

    def _back_to_issue(self, connection_id, key):
        self.jira.open_key(connection_id, key)
        self.open_provider("jira")

    def open_provider(self, provider, trail=()):
        """A provider in front. ``trail`` is the way it was reached, for its
        breadcrumb; opened any other way, it has none."""
        if provider in self.hosts:
            crumbs = getattr(self.views[provider], "crumbs", None)
            if crumbs is not None:
                crumbs.set_trail(trail)
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
        return self.cards.selected()

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
        """Each card's standing, and the totals over them, from what the views read."""
        tabs = {"jira": getattr(self, "changes", None), "teams": getattr(self, "teams_changes", None)}
        waits = {provider: tab.waiting() if tab is not None else 0 for provider, tab in tabs.items()}
        waiting = sum(waits.values())
        total, known = 0, True
        for provider, *_rest in PROVIDERS:
            view = self.views[provider]
            standing = standing_of(view.connections, view._listed, waiting=waits.get(provider, 0),
                                   kinds=provider == "agents")
            self.cards.cards[provider].set_standing(standing)
            if standing.count is None:
                known = False
            else:
                total += standing.count
        self.tiles["providers"].set_value("%d" % len(PROVIDERS))
        self.tiles["connections"].set_value("%d" % total if known else "—")
        self.tiles["waiting"].set_value("%d" % waiting, warn=bool(waiting))
