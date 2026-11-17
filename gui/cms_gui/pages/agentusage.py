"""Integrations -> Agents -> Usage: what agent work used and cost (QCR ADR-008 D8).

The figures are the core's (``agents usage``), summed from the project's
record of conversations - chats, tasks handed to other agents, Jira issues'
conversations, cycle steps - the same way for every view. Nothing is asked of
any agent or program to show them.

A cost is the agent program's own estimate, priced as its API would be: on a
subscription it is not what is charged. Codex reports none, so its turns are
counted as having no cost, never as free.
"""

import json
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel, QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget)

from .. import widgets
from .agentwords import cost_text, count_text
from .jiraconnections import _cell, _table

#: Period -> how far back, in seconds (None: everything).
PERIODS = (("7 days", 7 * 86400), ("30 days", 30 * 86400), ("All", None))
#: Breakdown -> the core's key, and what its column is called.
BREAKDOWNS = (("Model", "model"), ("Connection", "connection"), ("Source", "kind"), ("Role", "role"),
              ("Cycle", "cycle"))
#: What an estimated cost is - said where the figures are, on hover.
ESTIMATE_NOTE = ("The agent program's own estimate, priced as its API would be: on a subscription it is "
                 "not what is charged. Codex reports none.")
#: What a conversation came from, as a person would say it.
KIND_WORDS = {"chat": "Chats", "delegated": "Tasks for other agents", "message": "Questions to other agents",
              "jira": "Jira issues", "cycle": "Cycle steps", "record": "Other records"}


def _took(seconds):
    seconds = int(seconds or 0)
    if seconds < 60:
        return "%d s" % seconds
    if seconds < 3600:
        return "%d min" % (seconds // 60)
    return "%d h %d min" % (seconds // 3600, seconds % 3600 // 60)


#: What the period came to, a tile each: key, label, what it says on hover.
TILES = (("conversations", "Conversations", ""),
         ("turns", "Turns", ""),
         ("tokens", "Tokens", "In, cached and out together; thinking is part of out."),
         ("estimate", "Est. cost", ""),
         ("woken", "QAVector turns", "Turns QAVector continued by itself with what other agents came back "
                                     "with - nobody typed them."),
         ("unknown", "No cost reported", "Turns whose agent program reported no cost - counted apart, never as 0."))


def tile_values(totals):
    """Each tile's value, as it reads."""
    return {"conversations": str(totals.get("conversations") or 0), "turns": str(totals.get("turns") or 0),
            "tokens": count_text(totals.get("tokens_total") or 0),
            "estimate": cost_text(totals.get("estimate_usd")) if totals.get("estimate_usd") else "$0.00",
            "woken": str(totals.get("woken_turns") or 0), "unknown": str(totals.get("unknown_cost_turns") or 0)}


class StatTile(QFrame):
    """One figure of the period: a small caption over a large value."""

    def __init__(self, label, hint="", parent=None):
        super().__init__(parent)
        self.setProperty("role", "stattile")
        # Equal widths across the row, whatever each caption's length.
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        column = QVBoxLayout(self)
        column.setContentsMargins(12, 8, 12, 9)
        column.setSpacing(2)
        self.label = QLabel(label.upper())
        self.label.setProperty("role", "statlabel")
        column.addWidget(self.label)
        self.value = QLabel("—")
        self.value.setProperty("role", "statvalue")
        column.addWidget(self.value)
        if hint:
            self.setToolTip(hint)

    def set_value(self, text, warn=False):
        self.value.setText(text)
        if self.value.property("state") != ("warn" if warn else ""):
            self.value.setProperty("state", "warn" if warn else "")
            self.value.style().unpolish(self.value)
            self.value.style().polish(self.value)


class UsageTab(QWidget):
    HEADERS = ("", "Conversations", "Turns", "Tokens", "In (cached)", "Out", "Est. cost", "No cost", "Took")

    #: ``{"since": epoch or None}`` - the tab wants the core's figures.
    wanted = Signal(dict)
    #: (column of Activity, text) - somebody wants the turns behind a row.
    turns_wanted = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.payload = None
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 14, 0, 0)
        column.setSpacing(8)
        self.period = widgets.Segmented([name for name, _ in PERIODS], "30 days")
        self.period.changed.connect(lambda _name: self.shown())
        self.breakdown = widgets.Segmented([name for name, _ in BREAKDOWNS], "Model")
        self.breakdown.changed.connect(lambda _name: self._fill())
        self.export_button = QPushButton("Export…")
        self.export_button.setProperty("variant", "ghost")
        self.export_button.setToolTip("Save these figures as CSV (this breakdown) or JSON (everything).")
        self.export_button.clicked.connect(self._export)
        self.export_button.setEnabled(False)
        column.addWidget(widgets.row(self.period, self.breakdown, None, self.export_button, spacing=16))
        # What the period came to: a tile per figure, side by side.
        self.tiles = {key: StatTile(label, ESTIMATE_NOTE if key == "estimate" else hint)
                      for key, label, hint in TILES}
        strip = QHBoxLayout()
        strip.setSpacing(8)
        for tile in self.tiles.values():
            strip.addWidget(tile, 1)
        holder = QWidget()
        holder.setObjectName("usageFigures")
        holder.setLayout(strip)
        strip.setContentsMargins(0, 0, 0, 0)
        column.addWidget(holder)
        self.figures = holder
        panel = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        self.table = _table(self.HEADERS)
        self.table.setObjectName("usageTable")
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for index, width in ((1, 128), (2, 64), (3, 80), (4, 124), (5, 70), (6, 84), (7, 76), (8, 88)):
            self.table.setColumnWidth(index, width)
        self.table.itemDoubleClicked.connect(self._drill)
        self.search = widgets.SearchRow(self.table)
        self.search.changed.connect(self._say_empty)
        self.note = widgets.empty_note(self.table, "")
        self.skeleton = widgets.Skeleton(self.table, "rows")
        panel.layout().addWidget(self.table)
        column.addWidget(panel, 1)
        self.message = QLabel("")
        self.message.setProperty("role", "muted")
        self.message.setWordWrap(True)
        column.addWidget(self.message)
        self._fill()

    # -- the outside ------------------------------------------------------------
    def shown(self):
        seconds = dict(PERIODS)[self.period.current()]
        self.wanted.emit({"since": time.time() - seconds if seconds else None})

    def say(self, text):
        self.message.setText(text or "")

    def set_usage(self, payload):
        """The core's answer to ``agents usage``, or None for none yet."""
        self.payload = dict(payload) if isinstance(payload, dict) and payload.get("ok") else None
        self._fill()

    # -- inside -------------------------------------------------------------------
    def _key(self):
        return dict(BREAKDOWNS)[self.breakdown.current()]

    def _rows(self):
        parts = ((self.payload or {}).get("by") or {}).get(self._key()) or {}
        return sorted(parts.items(), key=lambda item: (-(item[1].get("estimate_usd") or 0),
                                                       -(item[1].get("tokens_total") or 0), item[0]))

    def _fill(self):
        self.table.setHorizontalHeaderLabels((self.breakdown.current(),) + self.HEADERS[1:])
        self.table.horizontalHeaderItem(6).setToolTip(ESTIMATE_NOTE)
        self.table.setRowCount(0)
        totals = self.payload or {}
        values = tile_values(totals) if self.payload else {}
        for key, tile in self.tiles.items():
            tile.set_value(values.get(key, "—"), warn=key == "unknown" and values.get(key, "0") != "0")
        self.export_button.setEnabled(bool(self.payload))
        for name, part in self._rows():
            row = self.table.rowCount()
            self.table.insertRow(row)
            tokens = part.get("tokens") or {}
            taken = sum(tokens.get(key) or 0 for key in ("input", "cache_read", "cache_write"))
            shown = KIND_WORDS.get(name, name) if self._key() == "kind" else name
            values = (shown, str(part.get("conversations") or 0), str(part.get("turns") or 0),
                      count_text(part.get("tokens_total") or 0),
                      "%s (%s)" % (count_text(taken), count_text(tokens.get("cache_read") or 0)),
                      count_text(tokens.get("output") or 0),
                      cost_text(part.get("estimate_usd")) if part.get("estimate_usd") else "",
                      str(part.get("unknown_cost_turns") or "") or "",
                      _took(part.get("seconds")))
            for index, value in enumerate(values):
                item = _cell(value)
                if index == 0:
                    item.setData(Qt.UserRole, name)
                if index == 7 and part.get("unknown_cost_turns"):
                    item.setToolTip("Turns whose agent program reported no cost - counted apart, never as 0.")
                self.table.setItem(row, index, item)
        self.search.apply()
        self._say_empty()

    def _say_empty(self):
        if self.payload is None:
            self.note.say("Open this tab to add up what agents used. Nothing is asked of any agent.")
        elif not self.payload.get("turns"):
            self.note.say("No agent turn in this period. Choose a longer one, or All.")
        elif self._key() == "role" and not self._rows():
            self.note.say("No task was handed to a role in this period.")
        elif self._key() == "cycle":
            self.note.say("No cycle step ran an agent in this period.")
        else:
            self.note.say("")
        self.note.say_filtered("Nothing matches the search." if self.search.searching() else "")

    def _drill(self, item):
        """The turns behind a model's or a connection's row, in Activity."""
        name = self.table.item(item.row(), 0).data(Qt.UserRole)
        column = {"model": "model", "connection": "connection"}.get(self._key())
        if column and name and name != "(default)":
            self.turns_wanted.emit(column, name)

    def _export(self):
        if not self.payload:
            return
        path, chosen = QFileDialog.getSaveFileName(
            self, "Export agent usage", "agent-usage.csv", "CSV - this breakdown (*.csv);;JSON - everything (*.json)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="") as handle:
                if path.lower().endswith(".json") or chosen.startswith("JSON"):
                    json.dump(self.payload, handle, indent=2, ensure_ascii=False)
                else:
                    handle.write(self.csv())
        except OSError as exc:
            self.say("Could not save %s: %s" % (path, exc))
            return
        self.say("Saved to %s." % path)

    def csv(self):
        """This breakdown as CSV: numbers as numbers, the estimate said as such."""
        import csv
        import io
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow([self.breakdown.current().lower(), "conversations", "turns", "tokens_total", "input",
                         "cache_read", "cache_write", "output", "thinking", "estimate_usd",
                         "unknown_cost_turns", "seconds"])
        for name, part in self._rows():
            tokens = part.get("tokens") or {}
            writer.writerow([name, part.get("conversations"), part.get("turns"), part.get("tokens_total"),
                             tokens.get("input"), tokens.get("cache_read"), tokens.get("cache_write"),
                             tokens.get("output"), tokens.get("thinking"), part.get("estimate_usd"),
                             part.get("unknown_cost_turns"), part.get("seconds")])
        return out.getvalue()
