# ADR-009: waking the conversation that delegated

Date: 2026-11-17. Status: **Accepted** (user, 2026-11-17: D1 the delegator
declares dependencies and when to wake it; D3 a setting of its own; D6 3 failed
rounds by default, the work never lost). Builds on
[ADR-006](adr-006-agent-messaging-and-delegation.md) (delegation, messages,
the chat's Other agents mode) and [ADR-008](adr-008-usage-and-cost.md)
(what turns cost).

## The problem

An agent that hands out tasks has two choices today. Neither is good.

* **It waits inside its own turn.** It calls `qavector_wait` (at most 60 s a
  call) over and over until the children end. The turn stays open, and every
  poll costs tokens. A long task can outlive the turn.
* **It ends its turn.** Then nothing continues it when the children end. Their
  results are on the cards, and a person has to come back and say "go on".

What delegation should do is let the agent hand out the tasks, say what it
started, and end its turn. When its tasks are done, the conversation goes on by
itself with their results. The tasks then run in parallel for as long as they
need, and nobody pays for waiting.

## What exists (audit)

* **Chat turns are the GUI's.** `ChatTab._start(session, text, document)`
  starts one, for any conversation, shown or not. A conversation with a turn
  running queues what is sent next and starts it when the turn completes.
* **Messages from other agents** wait in the conversation's inbox. They are put
  in front of the person's next message (`inbox_prefix`) and acknowledged then.
* **The host knows when a parent's turn ended:** the GUI revokes the turn's
  actor (`end_agent` -> `revoke`).
* **Delegation knows each child's parent** (`context["parent"]`, the actor
  `agent:<connection>/<session>`), its state, and its output: answer, changes,
  artifacts and criteria, all `unverified`.
* **Nothing records whether the parent saw a child's result** (through `wait`
  or `get`).

## Decisions (proposed)

**D1. The delegator says what depends on what** (user, 2026-11-17: "the
parent agent sets when to wake it - in effect a dependency").

* **Names.** A task or question may have a `name` of the agent's choosing
  (`tests`, `commit`), unique among the conversation's unfinished ones.
* **Dependencies between tasks.** `after: ["tests"]` keeps a task waiting until
  the named ones **succeed**. When one of them fails, is denied or is canceled,
  the task is not run: it ends `skipped`, saying why. Unknown names and cycles
  are refused when the task is asked for.
* **When to wake the delegator.** `agents.tasks.wake_when` registers a
  condition:
  * `{"all": [names]}` - when every named one has ended;
  * `{"any": [names]}` - when any of them has;
  * `on_failure: true` - also at once when one of them fails, without waiting
    for the rest.

  There may be several conditions; each fires once.
* **Default.** Without any condition, the **round** decides. The tasks and
  questions (`expects_reply`) that one turn hands on are that turn's round, and
  it wakes the delegator when every item in it has ended: succeeded, failed,
  denied, canceled, timed out or skipped.

Example: `tests` goes to one agent; `commit` goes to another with
`after: ["tests"]`; then `wake_when {"all": ["commit"], "on_failure": true}`.
The two never work in the folder at the same time. The delegator is woken once
when all is done, or at once if the tests fail.

**D2. When to wake.** The conversation is woken when its condition (D1) holds,
the turn that set it has ended, and at least one of the results it covers was
not **seen** by the parent. A result is seen when the parent's actor got the
execution in an ended state through `wait`, `get` or `events`; the host marks
it. An agent that waited for everything itself is not woken again for it.
Unsolicited messages from other agents do not wake a conversation; they keep
going with the next message, as now. Otherwise two agents could keep waking
each other.

**D3. Who decides.** It is a per-conversation setting, **When tasks finish**:

* **Continue** - the conversation goes on by itself;
* **Ask me** - a card "Tasks finished - Continue" waits in the conversation;
* **Off** - the results only show on the cards.

It is a setting of its own (user, 2026-11-17), not tied to the Other agents
mode. The connection holds the default for new conversations
(`wake_default`, beside `others_default`), and a conversation may change it. A
new connection starts with **Ask me**; existing ones get Ask me too.

**D4. What the woken turn is told.** It is a message written by QAVector, not
by the person, and the feed shows it as such ("QAVECTOR · 2 tasks finished").
It says which condition fired and what is still running. For each item that
ended it gives:

* the target, the model and effort, how it ended;
* the answer, bounded (2,000 characters each);
* the acceptance criteria, each `unverified`;
* how many files changed;
* the execution id, so the agent can read more.

Then it says what to do: check each criterion against evidence and record it
(`agents.tasks.verify`), carry on with what the person asked, and tell the
person what came of it. The record keeps the turn's `trigger`
(`tasks_finished`), so a woken turn is told apart from one the person started,
in the chat and in Usage.

**D5. Never over the person.** The woken turn does not start a separate turn
in two cases:

* a turn is running in the conversation - the results join the queue, in
  front of anything the person queued;
* the person has a message queued - the results go in front of it, as the
  inbox does.

A draft being typed does not hold the wake back; it stays a draft.

**D6. Bounds.** A woken turn may hand on another round, so rounds chain. The
limits are counted per message of the person, and the person's next message
resets them:

* **3 failed rounds.** These are wakes whose results include a failure, a
  skip, a denial or a timeout. After 3, the next wake becomes Ask me,
  whatever the setting. This stops a fix -> test fails -> fix loop going on
  while nobody looks.
* **10 wakes in all**, as a backstop for a chain that never fails and never
  ends.
* The existing limits stay: 3 children at once, depth 1. The budgets of
  ADR-008 U.5 will cap them by cost when they come.

Reaching a limit stops nothing and loses nothing. Running tasks go on.
Results are kept. The card's **Continue** sends exactly the message the
automatic wake would have sent, so the work picks up where it was. Each woken
turn is a normal turn: it is recorded, counted in Usage, and can be stopped
like any other.

**D7. Telling the agent.** An agent can only end its turn calmly if it knows
it will be continued. So:

* the `invoke` answer for `delegate` and for a question says what comes next
  (`next`): "Continue: end your turn when you have nothing else to do; this
  conversation goes on with the results when all the tasks of this turn have
  ended". With Ask me or Off it says the person decides, or to wait;
* the MCP text says the same in one line.

**D8. Where it runs.** The decision is in the core: the delegation adapter
keeps the rounds and the seen marks, and the host tells the GUI
`{"type": "round_finished", "session", "items": [...]}` when D2 holds. The
GUI starts the turn (D3, D5), because chat turns are the GUI's. If QAVector
closes, the children are canceled (ADR-006 D10), so nothing is left to wake.
Pending wakes are not kept across restarts.

## Increments

* **W.1** core: names, `after` and `skipped`, `wake_when`, rounds, seen
  marks, `round_finished`, `next` in the `invoke` answer, MCP text; together
  with two small changes: delegation to the agent's own connection with
  another model, and more than one task per target at once. Tests with fake
  agents.
  **Done 2026-11-17** (uncommitted):
  * `qcr/adapters/rounds.py` keeps items, names, rounds, conditions and seen
    marks.
  * The delegation adapter adds `name` and `after` (a task waits with a
    `waiting` event, or ends `failed` / `skipped`) and `agents.tasks.wake_when`.
  * There is a queue: at most `MAX_PER_TARGET = 2` tasks per agent connection
    at once; more wait instead of failing. `MAX_CHILDREN = 3` now counts
    waiting tasks too.
  * A task for the agent's own connection is allowed; the GUI grants it.
  * Tasks are no longer exclusive per target: `CapabilityDescriptor.exclusive`,
    whose default is True. Messages stay one per recipient at a time.
  * The host passes turn starts and ends (actor issued and revoked), ended
    executions and seen marks (`wait`, `get`), sends `round_finished`, and
    adds `next` to the answer of a delegation or a question. The policy carries
    `wake` (continue / ask / off), "ask" when not given.
  * Tests: `tests/test_qcr_wake.py`.

  The GUI acts on `round_finished` since W.2.
* **W.2** GUI. **Done 2026-11-17** (uncommitted):
  * the connection's **When tasks finish** (`wake_default`, Ask me by default)
    and the conversation's own choice (Settings -> TASKS: Continue / Ask me /
    Off), kept with the session (`wake`) and sent with the turn's policy;
  * `round_finished` in the chat: Continue starts the turn with QAVector's
    message (`trigger: tasks_finished`, shown as "QAVECTOR · TASKS FINISHED");
    a turn running puts it first in line; Ask me, and Continue past the
    limits (3 failed rounds, 10 in all since the person last spoke), is a
    TASKS FINISHED card with Continue / Dismiss; Off does nothing;
  * the turn's `trigger` in the record (`runner.send(trigger=)`).
  Tests: `gui/tests/test_wake.py`; light and dark screenshots checked.
* **W.3** (done 2026-11-17) the trigger in Usage: `woken_turns` in every
  total, and "N continued by QAVector" on Agents -> Usage.
* The user's live check: Opus hands Codex and a second Claude connection
  their tasks, ends its turn, and is continued with both results.

## For the user to decide

1. ~~The default~~ - decided: a setting of its own per connection (D3).
2. ~~The limit~~ - decided: 3 failed rounds, 10 wakes in all (D6).
3. ~~When to wake~~ - decided: the delegator declares it (D1).
