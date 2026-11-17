"""Integrations -> Jira -> Agent changes: what agents asked to change in Jira.

A comment an agent wants to post, a move to another status it wants made -
each waits for the person before anything is sent (QCR ADR-007). The same
change has a card in the agent's own conversation; this tab lists every
conversation's, so nothing waits unseen. Answering here or there is one answer.

Jira knows nothing of agents: the Integrations page adds this tab to the Jira
view, and the changes come from QAVector's core (``QcrHost.integration_changes``).
"""

import time

from PySide6.QtWidgets import QHeaderView, QVBoxLayout, QWidget

from .. import widgets
from .agentcards import CHANGE_STATES, IntegrationChangeCard, change_title
from .jiraconnections import _cell, _table

TITLE = "Agent changes"


class IntegrationChangesTab(QWidget):
    HEADERS = ("Asked", "Agent", "Change", "Issue", "Connection", "State")

    def __init__(self, provider="jira", parent=None):
        super().__init__(parent)
        self.provider = provider
        self.source = None
        self._ids = []
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 14, 0, 0)
        column.setSpacing(8)
        column.addWidget(widgets.lede(
            "Changes agents asked to make in Jira. Nothing is sent until you approve it; "
            "a comment can be changed first. Each also waits in its agent's conversation."))
        panel = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        self.table = _table(self.HEADERS)
        self.table.setObjectName("integrationChanges")
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        for index, width in ((0, 70), (1, 140), (3, 100), (4, 150), (5, 150)):
            self.table.setColumnWidth(index, width)
        self.table.itemSelectionChanged.connect(self._show_selected)
        self.search = widgets.SearchRow(self.table)
        self.search.changed.connect(self._say_empty)
        self.note = widgets.empty_note(
            self.table, "No agent has asked to change Jira. When one does - a comment, a move to "
                        "another status - it waits here for you.")
        panel.layout().addWidget(self.table)
        column.addWidget(panel, 1)
        self.holder = QVBoxLayout()
        self.holder.setContentsMargins(0, 0, 0, 0)
        column.addLayout(self.holder)
        self.card = None

    def set_source(self, source):
        """``source``: QAVector's core (``QcrHost``), or None."""
        self.source = source
        self.refresh()

    def _changes(self):
        found = (getattr(self.source, "integration_changes", None) or {}).values()
        prefix = "integrations.%s." % self.provider
        # Waiting ones first, newest first within each.
        return sorted((one for one in found if str(one.get("capability") or "").startswith(prefix)),
                      key=lambda one: (one.get("state") != "waiting", -(one.get("at") or 0)))

    def waiting(self):
        return sum(1 for one in self._changes() if one.get("state") == "waiting")

    def refresh(self):
        selected = self._selected_id()
        changes = self._changes()
        self.table.setRowCount(0)
        self._ids = []
        for change in changes:
            card = change.get("card") or {}
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = (time.strftime("%H:%M", time.localtime(change.get("at") or 0)), change.get("agent"),
                      change_title(card), card.get("key"),
                      card.get("connection_name") or card.get("connection"),
                      CHANGE_STATES.get(change.get("state"), change.get("state")))
            for index, value in enumerate(values):
                item = _cell(value)
                if index == 5 and change.get("state") == "waiting":
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                self.table.setItem(row, index, item)
            self._ids.append(change["execution_id"])
        self.search.apply()
        if selected in self._ids:
            self.table.selectRow(self._ids.index(selected))
        elif self._ids and not self.search.searching():
            self.table.selectRow(0)
        self._show_selected()

    def _selected_id(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return self._ids[rows[0].row()] if rows and rows[0].row() < len(self._ids) else None

    def _show_selected(self):
        if self.card is not None:
            for name in ("approve", "deny", "dismiss", "edited"):
                try:
                    getattr(self.card, name).disconnect()
                except (RuntimeError, TypeError):
                    pass
            self.card.hide()
            self.card.setParent(None)
            self.card.deleteLater()
            self.card = None
        execution_id = self._selected_id()
        change = (getattr(self.source, "integration_changes", None) or {}).get(execution_id)
        if change is None or self.source is None:
            return
        source = self.source
        self.card = IntegrationChangeCard(change, show_agent=True)
        self.card.approve.connect(lambda found: source.answer_integration(found, True))
        self.card.deny.connect(lambda found: source.answer_integration(found, False))
        self.card.dismiss.connect(source.dismiss_integration)
        self.card.edited.connect(source.set_integration_draft)
        self.holder.addWidget(self.card)

    def _say_empty(self):
        self.note.say_filtered("No change matches the search." if self.search.searching() else "")

    def title(self):
        count = self.waiting()
        return "%s (%d)" % (TITLE, count) if count else TITLE
