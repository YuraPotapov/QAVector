# ADR-013: budgets - the person sees what agent work costs, and decides (U.5)

Date: 2026-11-17. Status: **Accepted** (user, 2026-11-17).

* **Units:** dollars where the program reports them, and tokens. They are
  shown together as `$1.20 / 12k`, or `- / 12k` when no cost is known.
* **Scopes:** a task and a conversation.
* **Warnings:** yes, at 80%.
* **Default:** no budget. It is worth setting only where the provider charges
  per use, in USD. Builds on
[ADR-008](adr-008-usage-and-cost.md) (D7: soft and hard limits,
`best_effort`, nothing claims `enforced`), [ADR-009](adr-009-waking-the-delegator.md)
(waking) and [ADR-011](adr-011-follow-up-in-the-same-session.md) (no limit on
questions; the person stops a task).

## The problem

The person decides when agent work goes too far (user, 2026-11-17: no limit on
questions, the count is shown, the person stops it). To decide, they need to
see what the work has cost. They also need to be asked at the moment it
crosses an amount they set, not after the fact. Today:

* **Costs are not where decisions are made.** The est. cost of a conversation
  is in its header, and of a task in its history on Agents -> Tasks. The task
  cards and the board's list do not show it.
* **Nothing pauses work for a cost.** A dialogue between agents, or a chain of
  wakes, goes on whatever it has cost.

## What exists (audit)

* **Per turn:** the program's estimate (`cost_usd`, `cost_kind`: estimate,
  priced or unknown) and tokens, in the session record (ADR-008).
* **Codex reports no cost** (`unknown`, never 0). Its tokens are known.
* **`integrations.agents.usage`** sums one conversation as its own figures,
  and inclusive of the conversations it handed work to (`source.parent`).
  `usage.conversation(root, id)` gives both.
* **Where work can be held before it starts:**
  * a delegated task's answer turn (`agents.tasks.reply`, core);
  * a new delegation (core);
  * a woken turn of the delegating conversation (GUI, `round_finished`).

  The person's own message is never held: they chose to send it.

## Decisions (proposed)

**D1. What a budget is.** An amount of **est. cost (USD)**, of **tokens**, or
of both. Tokens are there because Codex reports no cost: a budget in USD alone
cannot see Codex's work. The board says so ("cost unknown - counted in
tokens"). Whichever amount is reached first counts.

**D2. Where it applies.**

* **A delegated task:** everything its child conversation used, over all its
  turns (questions and answers included).
* **A conversation:** inclusive - its own turns and everything it handed to
  other agents.

A connection holds the defaults for new tasks and conversations, beside
"Other agents" and "When tasks finish". A conversation may change its own in
Settings. By default there is **no budget**: nothing changes until the person
sets one.

**D3. What happens at the limit: the person is asked, nothing is cut off.**

* **80%:** a warning on the task's card, the conversation header and the
  board ("80% of its budget").
* **100%:** the next turn that would spend more waits, and a card asks:
  "**BUDGET REACHED** - $5.10 of $5.00 - Continue (budget +$5.00) / Stop". The
  turn may be:
  * the task's next answer turn;
  * a new delegation from the conversation;
  * a woken turn.

  Continue raises the budget by the same amount again. Stop ends the task
  (ADR-011's stop), or leaves the conversation's results on their cards.
* A running turn is not cut off midway. The existing Stop does that. The
  enforcement mode is said plainly: **best effort** - checked before a turn,
  so one turn may go past the limit.

**D4. A task's budget is never more than its conversation has left.** A task
inherits the smaller of its connection's task default and what its
conversation has left of its budget. The agent may set a lower one when it
delegates (`budget_usd`, `budget_tokens`), never a higher one.

**D5. Costs where decisions are made.**

* **Agents -> Tasks:** a Cost column (est. USD, or tokens when unknown), and
  "x% of budget" when there is one.
* **The task card in the chat:** the cost so far.
* Every decision to continue past a budget is kept in the task's history
  (who, when, the new amount).

## Increments

* **B.1** core: budgets in the connection and session records; the task's
  budget at delegation (D4); `budget_usd`/`budget_tokens` in the delegate
  input; holding the answer turn and new delegations at 100% (a
  `budget.requested` wait, like approval); the owner's `budget_answer`
  (continue with an amount, or stop); the board and the history carry cost and
  budget.
* **B.2** GUI: the connection's and the conversation's budgets; the 80% warning;
  the BUDGET REACHED card in the chat and on the board; the Cost column; the
  woken turn held at the conversation's budget.

## As built (2026-11-17, uncommitted)

* **B.1, core.**
  * `integrations/agents/budgets.py`: amounts, state (share, warn, reached),
    raise by the first amount, and the text.
  * A conversation's budget is kept beside its record in `budget.json`, so a
    turn that rewrites the record cannot lose it. Without one, the
    connection's default applies (`budget_task`, `budget_conversation`).
  * `agents budget <session>` reads it and sets it.
  * The delegation adapter gives a task its budget (D4). Its tool input
    takes `budget_usd` and `budget_tokens`.
  * Before an answer turn or a new task, the budget is checked
    (`_within_budget`). At the limit the turn is held with
    `budget.requested`. The host sends `budget_reached`, and the person's
    word comes back through `budget_answer` (go or stop).
  * `budget_raise` raises a conversation's budget at a woken turn.
  * `round_finished` carries the conversation's budget state.
  * The board and the history carry the cost and the budget.
  * Tests: `tests/test_qcr_budgets.py`.
* **B.2, GUI.**
  * `pages/agentbudget.py`: the budget fields (placeholders "No limit $" and
    "No limit tokens"; tokens typed as 250000, 250k or 1.5M) and the
    BUDGET REACHED card.
  * The connection's "Budget per task" and "Budget per conversation".
  * The chat:
    * the conversation's budget is in Settings -> Delegation -> Budget, with
      what it has spent beside it, in the warning ink past 80%;
    * the header shows "82% of $5.00 budget";
    * each task card shows its cost so far;
    * BUDGET REACHED cards appear for a held turn, and for a woken turn
      at the limit.
  * Agents -> Tasks has a Cost column, the "Reached its budget" state with
    Continue / Stop, and budget lines in the history.
* **The composer's settings (user, 2026-11-17).** Delegation's settings were
  hard to tell apart from Access, so they now sit in a framed group in
  Settings:
  * **Mode** (Off / Ask / Auto, the former "Other agents");
  * **Steps** (Off / Ask / Auto, the former "When tasks finish");
  * **Budget**.

  The whole Settings panel is one grid: captions of one width, fields of one
  width, and the delegation fields under the first column. Access stays by
  the message.
