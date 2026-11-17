# ADR-008: usage and cost accounting (plan §22)

Date: 2026-11-17. Status: **Accepted** for U.1-U.3 (user, 2026-11-17). The
user's decisions: Codex cost stays unknown (D4 not now); U.1-U.3 first, prices
and budgets later. The "est. API cost" wording (D3) was used as proposed; the
user did not object. Builds on
[ADR-005](adr-005-unified-agent-adapters.md) (normalized events) and
[ADR-006](adr-006-agent-messaging-and-delegation.md) (delegation, D9a).

## What exists (audit)

**One record already holds every agent call.** Every agent turn is written to
the project's agent-sessions record (`<project>/agent-sessions/`,
`integrations/agents/sessions.py`). That covers chats, delegated tasks and
answered messages (ADR-006), the Jira bridge, and cycle steps with agents
(`cycle/connections.record_agent_call`). A turn is one run of the agent's
program. It keeps `model`, `effort`, `mode`, `started`/`ended`, `state`,
`tokens`, `context {used, window}`, `cost_usd`, `cost_total_usd` and `steps`.
The session keeps `source {label, detail, ref, record, parent, by}`.

**What the providers report:**

| | Claude Code (stream-json) | Codex (app-server) |
|---|---|---|
| Tokens | the `result` event's `usage`: input (uncached), cache read, cache write, output, thinking - for the turn | `thread/tokenUsage/updated` total minus the thread's total before the turn: input (cached part taken out), cache read, output, reasoning; cache write when given |
| Per request | each assistant message's `usage` (context used) | each `tokenUsage.last` (context used) |
| Context window | `modelUsage.*.contextWindow` | `modelContextWindow` |
| Cost | `total_cost_usd`: the CLI's own figure for the whole conversation so far (`cost_scope="session"`); the runner keeps the turn's own as the difference | none (`cost_usd: null`) |
| Rate limits | not read | `account/rateLimits/read` exists, not read |

**How it is summed today.** It is summed separately in three places, each its
own way:

* the session (`sessions.settle`): turns' own cost; `None` when any turn's
  price is unknown; tokens summed;
* Agents → Activity (`pages/agents.py`): per turn;
* Jira issue → Summary (`pages/delegationtabs.py`): over the issue's
  conversations, found **by the issue's ref only**.

`tokens_total` adds input, cache read, cache write and output, and never adds
thinking again (it is part of output). The QCR journal has `usage.reported` only
for QCR tasks and delegations; chats do not go through QCR.

**Gaps found:**

1. **Cost is mislabelled.** Claude Code's `total_cost_usd` is the CLI's
   API-price estimate. On a Claude subscription it is not what is charged. It is
   shown as "cost". No source anywhere reports a billed amount.
2. **Codex has no cost at all**, and there are no prices anywhere in QAVector.
3. **Delegated work is missing from the Jira Summary.** A delegated task is a
   child conversation with its own ref (`qcr-task:<execution>`), so the Summary
   of the issue whose conversation delegated it does not count it. There is no
   inclusive "this conversation and what it handed on" figure anywhere.
4. **Cycle steps are not machine-attributable.** Their conversations say
   "Cycle X · step Y" and "Run <id>" as text for a person; there is no `ref`
   to sum by cycle or run. (Their `kind: claude` is right: a cycle step runs
   Claude only so far, `cycle/connections.AGENT_KINDS`.)
5. **Each view sums for itself.** There is no core API to ask "what did this
   project, connection, model, cycle or period use", and no QCR capability for
   agents or the GUI.
6. **No budgets or limits.** Delegation's model/effort choice (ADR-006 D9a) is
   unlimited by the user's decision until budgets exist.

## Decisions (proposed)

**D1. The unit of account is the turn, in the agent-sessions record.** That
record is the one place every consumer already writes to, so the second store
that §22 sketches (`ModelUsageRecord`) is not created. The turn is used rather
than each model request: Claude reports cost only per program run, and no
consumer needs a request-level price. Per-request usage stays in the turn's
events, for the context timeline. This deviates from §22's "request-level
source of truth"; the reason is that no provider gives request-level cost.

**D2. One token vocabulary, provider semantics fixed in the adapters.**
`input` = uncached input, `cache_read`, `cache_write`, `output` (which
includes `thinking`), `thinking` (a part of output, never added again). The
total is input + cache_read + cache_write + output. Each adapter documents its
mapping. Context occupancy is the last request's input side, never a sum of
turns (as now).

**D3. Cost says what it is.** A turn's figure gets a `cost_kind`:

* `estimate` - the program's own API-price estimate (Claude Code);
* `priced` - QAVector's from a pricing profile (D4);
* `unknown`.

The GUI says "est. API cost" and never "cost" alone. Nothing is called
billed; `billed` stays unsupported until some provider reports it.
Subscription allocation (§22 `allocated_cost`) is out of scope.

**D4. Prices are the user's, not the code's.** An optional per-machine
`pricing.json` holds profiles:

* `provider` and `model`;
* per-million prices for input, cache read, cache write and output;
* `effective_from`, `source` and a version.

It is used only for turns whose program reports no cost (Codex). A turn keeps
the profile version it was priced with. Changing a price does not change past
turns unless somebody asks for a recount. No prices ship in the code; they
change too often to be right in a release.

**D5. Attribution is written with the turn, not guessed later.** Each session
gets a machine-readable source:

* `kind`: chat, delegated, message, cycle, jira;
* `cycle_id`, `run_id`, `step_id` for cycle steps - a new `ref`
  `cycle:<cycle>:<run>:<step>` beside the readable label;
* `parent` and execution for delegation and messages (already there);
* the connection, provider, model and effort per turn (already there).

The kind is derived from the source for conversations recorded before.

**D6. One aggregation in core, with QCR reads.** A new
`integrations/agents/usage.py` sums turns from the record by project,
connection, provider, model, source kind, cycle/run, issue ref and period.
It gives two figures:

* **own** - the conversation's own turns;
* **inclusive** - plus its children, by `source.parent`.

They are never added to each other. Unknown prices stay unknown: the total says
how many turns had none and is not a smaller number. The three GUI views use it,
which fixes gap 3. QCR exposes it read-only:

* `analytics.usage.summary` (filters as above);
* `analytics.usage.conversation` (own and inclusive);
* `analytics.context.conversation` (the context timeline).

The permission is `analytics.read`, off for agents unless granted.

**D7. Budgets come last, and say how far they can be held.** Soft and hard
limits on tokens and est./priced cost, per connection, project or delegation.
Their enforcement mode is said plainly: `best_effort` - checked before a turn
or a delegation starts and between turns. A running turn is not cut off
mid-way, except by the existing Stop. Nothing claims `enforced`. The delegation
limits of ADR-006 option (b) land here: the highest effort and the models
another agent may choose for this one.

**D8. A usage view.** Agents → Usage shows:

* totals with filters (period, connection, model, source kind, project);
* breakdowns by model, connection, source and cycle;
* the drill-down conversation -> turns;
* context timelines;
* CSV/JSON export.

Every figure says when it is an estimate or partly unknown.

## Increments

* **U.1** (done 2026-11-17) `usage.py` with D2/D3 semantics; `cost_kind` on new turns (old
  turns read as `estimate` for Claude and `unknown` for others); cycle refs
  (D5); `agents usage` in the core's CLI; the Jira Summary says what was
  handed on, from the core's figures; "est." wording in the chat, Activity
  and Summary. The chat and Activity still show per-turn and per-conversation
  figures as before; the Usage view (U.3) uses the module.
* **U.2** (done 2026-11-17) QCR `analytics.usage.summary/.conversation/.ref`
  (`qcr/adapters/analytics.py`), registered once the host knows where the
  conversations are; the connection switch **Read agent usage and est. cost**
  (`usage`, off by default) grants `analytics.read`.
* **U.3** (done 2026-11-17) Agents -> Usage (`gui/cms_gui/pages/agentusage.py`):
  period, breakdown, totals, drill-down to Activity for a model or a
  connection, CSV/JSON export; light and dark checked. Not yet: the context
  timeline (D8) - Activity shows each turn's context; a timeline per
  conversation waits for a use.
* **U.4** pricing profiles (D4), for Codex first.
* **U.5** budgets and delegation limits (D7).

## For the user to decide

1. "est. API cost" in place of "cost", with Claude's figure called an estimate
   (D3).
2. Codex prices: entered by the user in `pricing.json` (D4), or left unknown.
3. Order: U.1-U.3 first, with prices (U.4) and budgets (U.5) after - or
   budgets earlier.
