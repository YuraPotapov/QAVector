"""The QCR Inspector (ADR-002 D10): the Log page's Executions mode.

What the QCR host has journalled for the project in use - every service action
it was asked for, by whom, and how it ended - and, for the one selected, what
happened step by step. Read-only, but for Cancel on an action still under way
(which stops the waiting, never the service).

Everything comes from the host: :meth:`QcrHost.ask` for the list and for one
action's events, and the host's push after every change keeps the list live
without asking again. Nothing here reads the journal file itself.

**No-data states**, each with its own words rather than one blank table: QCR
off, the host starting, the host down (with its reason), nothing journalled
yet, a search that matches nothing, nothing selected, and an action with no
events (which cannot happen - every action is written as ``queued`` first - so
it says so rather than looking empty). A missing value is "-".
"""

import json
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QHeaderView, QPushButton, QSplitter,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from .. import qcrhost, theme, widgets

EMPTY = "-"
#: How many of the newest actions are listed.
LIMIT = 200
COLUMNS = ("Time", "Target", "Action", "Asked by", "State", "Took", "Outcome")
C_TIME, C_SERVICE, C_ACTION, C_ACTOR, C_STATE, C_TOOK, C_OUTCOME = range(len(COLUMNS))
EVENT_COLUMNS = ("Time", "Event", "State", "Detail")
COLUMN_CAP = 260
ID_ROLE = Qt.UserRole + 51

#: What each state is called here, and the colour role it is drawn in.
STATES = {
    "queued": ("queued", None), "dispatched": ("taken by the page", None),
    "running": ("running", None), "succeeded": ("succeeded", "ok"),
    "failed": ("failed", "bad"), "timed_out": ("timed out", "bad"),
    "canceled": ("canceled", "warn"), "unknown": ("unknown", "warn"),
}

NOTE_OFF = ("QCR is off. Turn on “Run service actions through QCR” in Settings, "
            "and service actions are journalled here.")
NOTE_STARTING = "Starting the QCR host…"
NOTE_EMPTY = ("Nothing journalled in this project yet. Start, stop or restart a service "
              "that has a QCR identity, and it shows here.")
NOTE_FILTERED = "No action matches the search."
NOTE_NO_SELECTION = "Select an action above to see what happened, step by step."
NOTE_NO_EVENTS = "This action has no events - which should not happen: every action is written as queued first."


def _clock(stamp):
    if not stamp:
        return EMPTY
    return time.strftime("%H:%M:%S", time.localtime(float(stamp)))


def _date_time(stamp):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(stamp))) if stamp else ""


def _took(execution):
    start, end = execution.get("created_at"), execution.get("finished_at")
    if not start:
        return EMPTY
    if not end:
        return "under way"
    seconds = float(end) - float(start)
    return "%.2f s" % seconds if seconds < 10 else "%.0f s" % seconds


def actor_words(actor):
    """``gui-user`` -> "you", ``cycle:r1`` -> "cycle r1", ``run:h-3`` -> "run h-3"."""
    if actor == qcrhost.GUI_ACTOR:
        return "you"
    kind, _, rest = (actor or "").partition(":")
    return "%s %s" % (kind, rest) if rest else (actor or EMPTY)


def outcome(execution):
    """One line on how it ended, in the page's words."""
    state = execution.get("state")
    output, error = execution.get("output") or {}, execution.get("error")
    if state == "succeeded":
        status = output.get("service_status")
        return "service %s" % status if status else "done"
    if state == "unknown":
        return "the QCR host stopped before it finished"
    if error:
        return qcrhost.words(error)
    return EMPTY


def event_detail(event):
    """What one event says beyond its type and state, compact."""
    payload = event.get("payload")
    if event.get("type") == "state" and event.get("from"):
        moved = "from %s" % STATES.get(event["from"], (event["from"],))[0]
        return moved if payload is None else "%s; %s" % (moved, _compact(payload))
    return _compact(payload) if payload is not None else EMPTY


def _compact(value):
    text = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
    return text if len(text) <= 300 else text[:299] + "…"


def _table(columns):
    table = widgets.Table(0, len(columns), reclick_clears=False)
    table.setHorizontalHeaderLabels(list(columns))
    table.verticalHeader().setVisible(False)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setSortingEnabled(False)
    table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
    table.setShowGrid(False)
    table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    return table


def _fit(table, search, stretch):
    """Widths to the content under a ceiling; one column takes what is left."""
    table.resizeColumnsToContents()
    header = table.horizontalHeader()
    for column in range(table.columnCount()):
        table.setColumnWidth(column, min(max(table.columnWidth(column), 70), COLUMN_CAP))
        header.setSectionResizeMode(column, QHeaderView.Interactive)
    header.setSectionResizeMode(stretch, QHeaderView.Stretch)
    search.place()


def _item(text, *, mono=False, tone=None, tip=""):
    item = QTableWidgetItem(text if text not in (None, "") else EMPTY)
    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
    if mono:
        item.setFont(theme.mono_font())
    if text in (None, "", EMPTY):
        item.setForeground(theme.color(theme.NEUTRAL[500]))
    elif tone is not None:
        item.setForeground(theme.color({"ok": theme.OK, "bad": theme.BAD, "warn": theme.WARN}[tone]))
    if tip:
        item.setToolTip(tip)
    return item


class ExecutionsView(QWidget):
    """See the module docstring."""

    #: How many actions are listed and shown, for the Log page's line.
    counted = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._host = None
        self._name_of = lambda resource_id: ""
        self._rows = []                 # executions, newest first, as listed
        self._selected = None           # execution id whose events are shown
        self._asked = 0                 # newer list answers win over older ones
        #: A service's Executions button asked for its row: None, or whether the
        #: service is running (then its run's start is the row; else its newest).
        self._aim = None

        column = QVBoxLayout(self)
        column.setContentsMargins(24, 12, 24, 16)
        column.setSpacing(8)

        self.count = widgets.mono("")
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        self.cancel_button = QPushButton("Cancel action")
        self.cancel_button.setToolTip("Stop waiting for the selected action. The service is not stopped.")
        self.cancel_button.clicked.connect(self._cancel)
        self.cancel_button.setEnabled(False)
        column.addWidget(widgets.row(widgets.kicker("service actions"), self.count, None,
                                     self.cancel_button, self.refresh_button))

        self.table = _table(COLUMNS)
        self.note = widgets.empty_note(self.table, NOTE_OFF)
        self.search = widgets.SearchRow(self.table)
        self.search.changed.connect(self._searched)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        list_panel = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        list_panel.layout().addWidget(self.table)

        self.events = _table(EVENT_COLUMNS)
        self.events_note = widgets.empty_note(self.events, NOTE_NO_SELECTION)
        self.events_search = widgets.SearchRow(self.events)
        events_panel = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        events_panel.layout().addWidget(self.events)
        # Said above the second table, so it never reads as more of the first.
        steps = QWidget()
        steps_column = QVBoxLayout(steps)
        steps_column.setContentsMargins(0, 10, 0, 0)
        steps_column.setSpacing(8)
        steps_column.addWidget(widgets.kicker("steps of the selected action"))
        steps_column.addWidget(events_panel, 1)

        split = QSplitter(Qt.Vertical)
        split.setChildrenCollapsible(False)
        split.addWidget(list_panel)
        split.addWidget(steps)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        column.addWidget(split, 1)
        _fit(self.table, self.search, C_OUTCOME)
        _fit(self.events, self.events_search, 3)

    # -- wiring ---------------------------------------------------------------------
    def set_host(self, host, name_of=None):
        """``host``: the main window's QcrHost. ``name_of(resource_id)`` names a service."""
        self._host = host
        if name_of is not None:
            self._name_of = name_of
        host.execution_changed.connect(self._changed)
        host.state_changed.connect(lambda _state: self._host_state())
        self._host_state()

    def show_service(self, label, running=False):
        """Narrow the list to one service, by what its Service cell says, and
        select the row that explains it now: while it ``running``, the start or
        restart its current run came from; otherwise its newest action. Whatever
        was selected before is not kept. Applied when the list arrives, if it
        has not yet."""
        box = self.search.box(C_SERVICE)
        if box is not None:
            box.setText(label)
        self._aim = bool(running)
        self._select_aimed()

    def _select_aimed(self):
        if self._aim is None:
            return
        visible = [row for row in range(self.table.rowCount()) if not self.table.isRowHidden(row)]
        if not visible:
            return                      # nothing listed yet: kept for when it is
        pick = visible[0]
        if self._aim:
            for row in visible:         # newest first, so the first is its current run's
                action, state = self.table.item(row, C_ACTION), self.table.item(row, C_STATE)
                if action.text() in ("start", "restart") and state.text() == STATES["succeeded"][0]:
                    pick = row
                    break
        self._aim = None
        self.table.selectRow(pick)
        self.table.scrollToItem(self.table.item(pick, C_TIME))

    # -- the list -------------------------------------------------------------------
    def _host_state(self):
        state = self._host.state if self._host is not None else qcrhost.OFF
        if state == qcrhost.READY:
            self.refresh()
            return
        self._rows = []
        self._render()
        self.note.say({qcrhost.OFF: NOTE_OFF, qcrhost.STARTING: NOTE_STARTING}.get(
            state, (self._host.problem if self._host is not None else "") or NOTE_OFF))

    def refresh(self):
        """Ask the host for the newest actions again."""
        if self._host is None or self._host.state != qcrhost.READY:
            self._host_state()
            return
        self._asked += 1
        asked = self._asked

        def answered(reply):
            if asked != self._asked:
                return                  # a newer list is on its way
            if not reply.get("ok"):
                self._rows = []
                self._render()
                self.note.say(qcrhost.words(reply.get("error")))
                return
            self._rows = list(reply.get("executions") or ())
            self.note.say(NOTE_EMPTY)
            self._render()
        self._host.ask("list", answered, filters={"limit": LIMIT})

    def _changed(self, execution):
        """The host's push: one action changed. Kept in place, or put on top."""
        execution_id = execution.get("execution_id")
        if not execution_id:
            return
        for index, row in enumerate(self._rows):
            if row.get("execution_id") == execution_id:
                self._rows[index] = execution
                break
        else:
            self._rows.insert(0, execution)
            del self._rows[LIMIT:]
        self.note.say(NOTE_EMPTY)
        self._render()
        if execution_id == self._selected:
            self._load_events(execution_id)

    def _render(self):
        table = self.table
        table.blockSignals(True)
        table.setRowCount(len(self._rows))
        for index, execution in enumerate(self._rows):
            state = execution.get("state") or ""
            words, tone = STATES.get(state, (state, None))
            op = (execution.get("capability_id") or "").rsplit(".", 1)[-1]
            service = self._name_of(execution.get("resource_id") or "") or (execution.get("resource_id") or "")[:8]
            cells = (
                _item(_clock(execution.get("created_at")), mono=True, tip=_date_time(execution.get("created_at"))),
                _item(service, tip=execution.get("resource_id") or ""),
                _item(op),
                _item(actor_words(execution.get("actor_id")), tip=execution.get("actor_id") or ""),
                _item(words, tone=tone),
                _item(_took(execution), mono=True),
                _item(outcome(execution), tip=((execution.get("error") or {}).get("message") or "")),
            )
            cells[0].setData(ID_ROLE, execution.get("execution_id"))
            for column_index, cell in enumerate(cells):
                table.setItem(index, column_index, cell)
            if execution.get("execution_id") == self._selected:
                table.selectRow(index)
        table.blockSignals(False)
        _fit(table, self.search, C_OUTCOME)
        self.search.apply()
        self._update_count()
        self._update_cancel()
        self._select_aimed()

    def _searched(self):
        self.note.say_filtered(NOTE_FILTERED if self.search.searching() else "")
        self._update_count()

    def _update_count(self):
        shown = sum(1 for row in range(self.table.rowCount()) if not self.table.isRowHidden(row))
        total = len(self._rows)
        text = "" if not total else ("%d" % total if shown == total else "%d of %d" % (shown, total))
        self.count.setText(text)
        self.counted.emit(text)

    # -- one action ---------------------------------------------------------------------
    def _selected_execution(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        item = self.table.item(rows[0].row(), C_TIME)
        execution_id = item.data(ID_ROLE) if item is not None else None
        return next((row for row in self._rows if row.get("execution_id") == execution_id), None)

    def _selection_changed(self):
        execution = self._selected_execution()
        self._selected = execution.get("execution_id") if execution else None
        self._update_cancel()
        if execution is None:
            self.events.setRowCount(0)
            self.events_note.say(NOTE_NO_SELECTION)
            return
        self._load_events(self._selected)

    def _load_events(self, execution_id):
        if self._host is None:
            return

        def answered(reply):
            if execution_id != self._selected:
                return
            if not reply.get("ok"):
                self.events.setRowCount(0)
                self.events_note.say(qcrhost.words(reply.get("error")))
                return
            self._show_events(reply.get("events") or ())
        self._host.ask("events", answered, execution_id=execution_id)

    def _show_events(self, events):
        table = self.events
        table.setRowCount(len(events))
        for index, event in enumerate(events):
            state = event.get("state") or ""
            words, tone = STATES.get(state, (state, None)) if state else ("", None)
            kind = event.get("type") or ""
            cells = (_item(_clock(event.get("at")), mono=True, tip=_date_time(event.get("at"))),
                     _item("state" if kind == "state" else kind.replace("owner.", "page: ")),
                     _item(words, tone=tone),
                     _item(event_detail(event), mono=True, tip=event_detail(event)))
            for column_index, cell in enumerate(cells):
                table.setItem(index, column_index, cell)
        self.events_note.say(NOTE_NO_EVENTS)
        _fit(table, self.events_search, 3)
        self.events_search.apply()

    def _update_cancel(self):
        execution = next((row for row in self._rows if row.get("execution_id") == self._selected), None)
        self.cancel_button.setEnabled(bool(execution) and execution.get("state") not in qcrhost.TERMINAL)

    def _cancel(self):
        if self._host is None or not self._selected:
            return
        execution_id = self._selected

        def answered(reply):
            if not reply.get("ok"):
                self.events_note.say(qcrhost.words(reply.get("error")))
        self._host.ask("cancel", answered, execution_id=execution_id,
                       reason="Canceled from the Executions view.")
