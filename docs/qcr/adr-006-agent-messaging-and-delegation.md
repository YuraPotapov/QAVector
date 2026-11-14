# ADR-006: agent messaging and task delegation (Phase 5)

Date: 2026-11-13. Status: **Accepted** (user, 2026-11-13); 5.1-5.4 implemented ([5.1](phase-5-1-report.md), [5.2](phase-5-2-report.md), [5.3](phase-5-3-report.md), [5.4](phase-5-4-report.md)); 5.5 is the user's live check. User decisions 2026-11-13: D2 (parent's mode), D3/D4 (chat mode Auto / Approve / Deny; default and allowed agents on the connection). The rest await approval.
Builds on [ADR-005](adr-005-unified-agent-adapters.md). Phase 4's live check is
deferred by the user's decision; Phase 5 starts without its gate closed.

## What exists (audit)

* `qcr.adapters.agents.AgentRuntime` registers `agents.<profile>.describe/execute`
  on the shared Registry/Engine. One execution is one turn. The runner,
  journal, cancel, idempotency, timeout and raw logs are reused. Profiles come
  only over the trusted owner pipe (`agent_profiles`); **the GUI never sends
  any**, so today no agent can reach another.
* An agent turn reaches QCR through the per-turn MCP server. Its actor is
  `agent:<connection>/<session>`, and its grants come from the connection's
  "QAVector access" switches (services, scenarios, cycles, approvals, jira).
  The token is revoked when the turn ends. The actor string is the same in
  every turn of a session, so a later turn can still read an execution an
  earlier turn started (`Engine._visible` matches the actor).
* Sessions carry an opaque `source` (`label, detail, ref, record`). `record`
  makes a conversation read-only in the chat, which is what the Jira bridge uses.
* Nothing stores messages between agents. Nothing limits depth, hops or the
  number of child tasks.

## Decisions

**D1. A delegation target is an agent connection, not a profile.** The
delegated agent works in the delegating session's folder, under its
restrictions. No profile editor is needed (that stays Phase 7).
`AgentRuntime` profiles stay for owner-configured tasks.

**D2. A child never has more than its parent.** Mode = the one the task asks
for, capped by the parent session's mode; **the parent's mode when not said**
(user, 2026-11-13).

*Changed 2026-11-14 after the first live delegation.* Claude asked Codex to
start a service through QAVector, and the child, which had no QAVector access,
could not. A child turn is now given, for that one turn, the delegating
conversation's own grants that the target connection's "QAVector access"
switches also allow (`store.ACCESS_GRANTS`). It never gets `agents.*`, so it
still cannot delegate or message further: depth 1. The host issues the child's
actor (`agent:<target>/<child session>`) and token, and revokes both when the
turn ends. The package's `constraints.qavector_access` lists what the child
has, and its message says so.

**D3. A chat's "Other agents" mode decides (user, 2026-11-13).** It sits beside
the chat's access mode and is per conversation:

* **Auto**: the agent delegates tasks to other agents and messages them, and
  both run without asking (a message that asks for an answer starts the
  recipient's turn).
* **Approve**: every delegation and every message waits in this chat as a
  card (target, goal or text, mode, Approve / Deny). Nothing runs and nothing
  is spent until the person approves. The agent's turn waits for the answer
  (`awaiting_approval`). A denial is the task's result.
* **Deny**: this conversation works with its own provider only. Its turns are
  given no `agents.*` grants, and it accepts no messages from other agents.

**Where the defaults and targets live: the connection (user, 2026-11-13).**
An agent connection gets two settings:
* **Other agents, by default**: Auto, Approve or Deny. A new conversation of
  that connection starts with it, and the person may change it per
  conversation.
* **Allowed agents**: a checklist of the *other* connections of this
  QAVector, each with its provider. The connection itself is never listed.
  Delegation and messages may go only to the ticked ones, in every mode.

Defaults:
* A new connection gets Approve and every other existing connection ticked.
* A connection added later is not ticked anywhere on its own.
* Connections and conversations from before this change are Deny with
  nothing ticked, so nothing about them changes.

The mode is kept in the session, like its access mode and folder, and a turn's
grants follow it: Auto and Approve add `agents.delegate` and `agents.message`
to the turn's actor, and the turn sees only the allowed targets. Delegated
(child) conversations are always Deny (D2).

**D4. Messages follow the same mode.** `agents.send_message` stores a message
durably (`queued`). What happens next depends on the sender's chat mode:
* **Approve:** the person approves first.
* **Auto:** the message is delivered at once.

Then, by what the message asks:
* **No answer asked for:** the message appears as a card in the recipient's
  chat and is put at the top of that chat's next turn.
* **An answer asked for (`expects_reply`):** this is a one-turn task for the
  recipient (D5), and its reply comes back to the sender with `agents.reply`.

A recipient chat in Deny refuses messages (`recipient_unavailable`). States:
`queued -> awaiting_approval -> delivered -> acknowledged`, or `denied`,
`failed`, `expired`. Delivery is not an answer.

**D4 as built (5.3).** A message goes to a **connection's inbox** (its
recipient is a connection). A reply goes back to the conversation that wrote.
* **Capability:** `agents.<id>.message` (`agents.message` on resource
  `agent-messages~<id>`), apart from the delegation resource, so a message
  never waits behind a task.
* **Answer asked (`expects_reply`):** one read-only turn of the recipient in a
  record conversation (`source.label = "Message"`). Its answer is stored as a
  reply, and the original is `acknowledged`.
* **Reply:** `reply_to` answers a message addressed to the sender's
  connection. The reply goes to the original sender's conversation, in the same
  thread (`conversation_id`).
* **No separate tools.** Agents get no list or reply tools: incoming messages
  reach an agent through its chat (5.4).
* **Owner pipe:** the GUI reads the store with `messages` and acknowledges
  with `message_ack`.
* **Store and limits:** the store is `<project>/.qcr/messages.sqlite` on the
  journal primitives. A message's id is its sending execution's id. Unread
  messages expire after 7 days. A recipient connection whose default is Deny
  refuses (`recipient_unavailable`).
* **Approve:** one message per recipient waits for approval at a time; the
  next gets `busy`.

**D5. Delegation is a task, and starting it is explicit.**
`agents.<connection>.delegate` creates a child session and runs one turn. It
returns an ordinary execution handle. The parent follows it with the existing
`qavector_wait` / `qavector_events` / `qavector_cancel`. `await_task` is just
the Engine's wait. The child session's `source` is
`{label: "Delegated", detail: "<parent connection>: <goal>", ref: "qcr-task:<task_id>", record: true}`.
A person reads it and can stop it, but does not talk in it.

**D6. Limits** (each a constant, refused with `limit_exceeded` before
anything runs):
* depth 1 (D2);
* at most 3 running child tasks per parent session, and - because the Engine
  runs one unfinished execution per resource - at most one running task per
  target connection at a time (a second gets `busy`): two children of one
  provider never edit the same folder at once;
* 30 minutes default and 6 h maximum per task;
* at most 20 messages per sender session per hour;
* a message body of at most 20 000 characters;
* a context package of at most 64 KB.

Retries are never automatic. A failed delegation is reported, and re-asking it
is the parent's decision. An idempotency key dedups.

**D7. Context Package, not transcript.** This is an immutable JSON document,
content-addressed in `<project>/.qcr/context/<sha256>.json`
(`qcr/context.py`). It holds:
* goal, acceptance criteria and constraints (mode, time, depth 1, no
  QAVector access);
* a summary the delegator writes (≤ 20 000 chars);
* files: path inside the folder, sha256 and size at packaging time, and the
  folder's git `HEAD` when it is a repository;
* artifacts: the delegator's own executions, with capability, state and a
  2 000-character output excerpt (the child cannot read QCR itself);
* provenance: actor, session, connection, execution, time.

Limits: at most 50 files and 20 artifacts, 64 KB in all.

**Secrets.** QAVector had no pattern-based detector, only replacement of known
values (an earlier draft of this ADR said otherwise). So a package is checked
in two ways:
* against every value in the secrets store (8 characters or longer);
* against a few high-confidence credential shapes: private keys, `sk-`,
  `ghp_`/`github_pat_`, `AKIA`, `xox*-`, JWT.

A match refuses the task with `secret_in_context`, naming the field and never
the value. The goal, criteria, summary and file list are checked in
`requirements()`, before the Engine records the request, so a refused secret
is not in the journal either.

**Freshness.** At launch, which may come after an approval wait, the files are
hashed again. A changed file is marked "(changed since the task was given)" in
the child's message and listed in the output's `stale_files`. The child gets
the package rendered as text, read back from its file and checked against its
hash. Provider session ids are never passed.

**D8. Result = evidence, not "done".** A task's output holds:
* the child's final text and its turn state;
* the changed-files summary from the existing change tracking;
* the turn artifacts;
* every acceptance criterion marked `unverified`.

The Engine's `succeeded` means only that the turn completed. Verifying is the
delegator's step, through its own QCR tools (for example a scenario run), and
it records that verification with `agents.tasks.<id>.verify` (criterion,
verdict, evidence execution id). A task is shown as **verified** only when every
criterion has a recorded passing verdict.

**D9. Visibility.**
* The parent chat shows a card for each delegation: target, goal, state,
  "Open". Messages appear as cards in both sessions.
* Delegated and messaged executions are in the Log → Executions inspector,
  with a correlation id (the root execution) and a hop count.
* Agents → Activity lists messages and tasks.

**D10. Where it runs.** The child turn runs in the project's QCR host, which
the GUI owns, as the Phase 4 runtime already does. Closing QAVector cancels
running children. On the next start their executions reconcile to `unknown`
and are never resubmitted.

## Increments

* **5.1 Delegation core.** `agents.<connection>.delegate` + `agents.tasks.<id>.verify`
  in the host, D1/D2/D5/D6/D8/D10, with the D3 modes as the host sees them
  (grants per mode; Approve as `awaiting_approval` answered over the owner pipe). The context package (D7) has a minimal
  goal, criteria and summary. Tests with fake Claude and Codex CLIs through the
  real host, socket and MCP.
* **5.2 Context packages.** Full D7: file and artifact references,
  provenance, redaction, size and freshness checks.
* **5.3 Messaging.** Durable store (`journal` primitives, project folder),
  `send_message / list_messages / reply`, state machine
  `queued -> delivered -> acknowledged | failed | expired`, dedup by
  `message_id`, rate limit (D4/D6).
* **5.4 GUI.** The connection's default mode and allowed agents, the chat's Other agents mode (D3) with its approval cards, chat cards, a read-only delegated
  session, Activity rows, inspector correlation (D9).
* **5.5 Gate 2, by the user.** A real Claude -> Codex delegation with
  verification, Codex -> Claude, and a message without a task.

## Not in this phase

Delegation deeper than one level, automatic replies to messages, a shared
task board and a profile editor (Phase 7), cost budgets (Phase 22: Codex
reports no price, so a cost limit cannot be enforced across providers yet).
