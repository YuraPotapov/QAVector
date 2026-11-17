"""Budgets in the window (QCR ADR-013): set them, see them, answer them.

* :class:`BudgetFields` - an amount of dollars, of tokens, or both; empty is
  "no budget". On a connection (the defaults for a task it hands on and for
  one of its conversations) and in a conversation's Settings.
* :func:`spend_text` - ``$1.20 / 12k``, or ``- / 12k`` when nothing reported a
  cost (Codex): dollars where the program says them, tokens always.
* :class:`BudgetCard` - a turn waits at a budget: Continue raises it by what it
  was first set to, Stop ends it. Nothing is cut off midway; the card says so.

A budget is only worth setting where the provider charges per use (an API
key): on a subscription the est. cost is not what is charged.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from .. import widgets
from .agentwords import cost_text, count_text

#: Said wherever a budget is set: when it is worth it, and how far it holds.
BUDGET_NOTE = ("Worth setting where the provider charges per use. Checked before each turn, so one turn "
               "may go past it; at the limit you are asked to continue or stop. Empty: no budget.")


def spend_text(spent):
    """``$1.20 / 12k``: est. dollars where reported, tokens always."""
    spent = spent or {}
    usd = spent.get("usd") or 0
    return "%s / %s" % (cost_text(usd) if usd else "-", count_text(int(spent.get("tokens") or 0)) or "0")


def budget_text(budget):
    """``$5.00 / 100k``, ``$5.00``, ``100k tokens`` - or "" for none."""
    budget = budget or {}
    parts = []
    if budget.get("usd"):
        parts.append(cost_text(budget["usd"]))
    if budget.get("tokens"):
        parts.append(count_text(int(budget["tokens"])) + ("" if parts else " tokens"))
    return " / ".join(parts)


def share_text(found):
    """``82% of $5.00`` for a budget state from the core, or ""."""
    if not found or found.get("share") is None:
        return ""
    return "%d%% of %s" % (round(found["share"] * 100), budget_text(found.get("budget")))


class BudgetFields(QWidget):
    """Dollars and tokens, side by side; either may stay empty. Tokens as typed
    (250000), or with k / M (250k, 1.5M)."""

    changed = Signal()

    def __init__(self, budget=None, parent=None, width=105):
        super().__init__(parent)
        across = QHBoxLayout(self)
        across.setContentsMargins(0, 0, 0, 0)
        across.setSpacing(6)
        self.usd = QLineEdit()
        self.usd.setPlaceholderText("No limit $")
        self.usd.setToolTip("Dollars, as 5 or $5.")
        self.usd.setAccessibleName("Budget in dollars")
        self.tokens = QLineEdit()
        self.tokens.setPlaceholderText("No limit tokens")
        self.tokens.setAccessibleName("Budget in tokens")
        self.tokens.setToolTip("Tokens, as 250000, 250k or 1.5M.")
        for field in (self.usd, self.tokens):
            field.setFixedWidth(width)
            across.addWidget(field)
            field.textChanged.connect(self.changed)
        widgets.see_through(self)
        self.set_budget(budget)

    def set_budget(self, budget):
        budget = budget or {}
        self.usd.setText(("$%g" % budget["usd"]) if budget.get("usd") else "")
        self.tokens.setText(count_text(int(budget["tokens"])) if budget.get("tokens") else "")

    def budget(self):
        """``{"usd": 5.0, "tokens": 100000}`` with what is filled in; ``{}``: none."""
        made = {}
        try:
            usd = float(self.usd.text().replace("$", "").replace(",", ".").strip() or 0)
        except ValueError:
            usd = 0
        tokens = tokens_of(self.tokens.text())
        if usd > 0:
            made["usd"] = round(usd, 2)
        if tokens > 0:
            made["tokens"] = tokens
        return made


def tokens_of(text):
    """``250000``, ``250k``, ``1.5M`` -> a count; anything else -> 0."""
    text = str(text or "").strip().lower().replace(",", ".").replace(" ", "")
    scale = 1
    if text.endswith("k"):
        scale, text = 1000, text[:-1]
    elif text.endswith("m"):
        scale, text = 1000000, text[:-1]
    try:
        return max(int(float(text) * scale), 0) if text else 0
    except ValueError:
        return 0


class BudgetCard(QFrame):
    """A turn waits at a budget: the person's word."""

    #: (key, go on)
    answered = Signal(str, bool)

    def __init__(self, key, found, parent=None):
        super().__init__(parent)
        self.setProperty("role", "agentcard")
        self.key = key
        self.found = dict(found)
        column = QVBoxLayout(self)
        column.setContentsMargins(10, 6, 10, 8)
        column.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.kind = QLabel("BUDGET REACHED")
        self.kind.setProperty("role", "msgkind")
        head.addWidget(self.kind)
        self.meta = QLabel("waits for you")
        self.meta.setProperty("role", "msgmeta")
        head.addWidget(self.meta)
        head.addStretch(1)
        column.addLayout(head)
        scope = found.get("scope") or "conversation"
        whose = "This conversation, with what it handed on," if scope == "conversation" else \
            "The task for %s" % (found.get("target_name") or "another agent")
        self.text = QLabel("%s has spent %s of its %s." % (
            whose, spend_text(found.get("spent")), budget_text(found.get("budget")) or "budget"))
        self.text.setWordWrap(True)
        self.text.setProperty("role", "cardquestion")
        column.addWidget(self.text)
        then = {"task": "Its next turn waits.", "conversation": "Its next turn waits.",
                "wake": "QAVector does not go on with the results by itself until you say so."}
        self.note = QLabel("%s Continue raises the budget by %s. Checked before each turn, so one turn may "
                           "go past it." % (then.get(found.get("why") or scope, then["task"]),
                                            budget_text(_step(found.get("budget"))) or "its amount"))
        self.note.setProperty("role", "hint")
        self.note.setWordWrap(True)
        column.addWidget(self.note)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setToolTip("End it here. What it found stays.")
        self.stop_button.clicked.connect(lambda: self._answer(False))
        self.go_button = QPushButton("Continue")
        self.go_button.setProperty("variant", "primary")
        self.go_button.clicked.connect(lambda: self._answer(True))
        self.actions = widgets.row(None, self.stop_button, self.go_button)
        column.addWidget(self.actions)
        widgets.see_through(self)

    def _answer(self, go):
        self.go_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.meta.setText("continued" if go else "stopped")
        self.actions.setVisible(False)
        self.setProperty("settled", "true")
        self.style().unpolish(self)
        self.style().polish(self)
        self.answered.emit(self.key, go)


def _step(budget):
    budget = budget or {}
    return {key: budget.get("step_" + key) or budget.get(key) for key in ("usd", "tokens") if budget.get(key)}
