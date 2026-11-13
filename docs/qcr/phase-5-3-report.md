# QCR — Phase 5.3: messages between agents

Date: 2026-11-14. Branch: `codex/phase-4-unified-agent-adapter`, on top of 5.2 (`5e6a112`).
Design: [ADR-006 D4, "as built"](adr-006-agent-messaging-and-delegation.md).

## Implemented

* `qcr/messages.py` (`MessageStore`) runs on the journal primitives in
  `<project>/.qcr/messages.sqlite`.
  * States: `queued -> awaiting_approval -> delivered -> acknowledged`, or
    `denied` / `failed` / `expired`. Transitions only go forward, and a message
    ends once.
  * A message is recorded once per id (same content: no-op; other content:
    `conflict`). Listings filter by recipient connection, recipient
    conversation, sender or thread. An unread message expires after 7 days.
* `agents.<id>.message` (`agents.message` on `agent-messages~<id>`) follows the
  same policy as delegation (Auto / Approve / Deny, never its own connection),
  plus:
  * a recipient connection whose default is Deny refuses
    (`recipient_unavailable`);
  * 20 messages per conversation per hour;
  * the body is checked for secrets before anything is recorded.
* **No reply asked:** the message is delivered to the recipient connection's
  inbox and nothing runs: no turn, no tokens.
* **`expects_reply`:** one read-only turn of the recipient in a record
  conversation (`Message`, Deny, no QAVector access) answers it. The answer is
  stored as a reply to the asking conversation, and the original becomes
  `acknowledged`. The execution output carries the reply.
* **`reply_to`:** only for a message addressed to the replier's connection by
  the target. The reply goes back to the conversation that wrote, in its
  thread.
* **Approve:**
  * the message waits as `awaiting_approval`, and the host sends
    `agent_approval` with `kind: "message"`;
  * a denial makes the message `denied`, and cancel or close make it `failed`;
  * one waiting message per recipient at a time; the next gets `busy`.
* **Owner pipe for the GUI (5.4):** `messages` (listing) and `message_ack`
  (the person passed a delivered message into a chat).

## Validation

* `tests/test_qcr_messages.py`: **11 passed**. The store tests cover dedup,
  conflict, transitions, terminal state, expiry and filters. Through the real
  host: inbox without a turn, a question answered by one read-only turn, a
  reply routed back, approve, deny, cancel, a recipient that works alone, own
  connection, rate, secrets with nothing journaled, and the store's place.
* Delegation and context tests stay green: **38 passed** together with the
  message tests.
* Core selection (agents, QCR, journal, packaging, cycle agent connections, plugins and secrets): **891 passed, 1 skipped**.

Not run: a real model turn, the full repository suite.

## Next

* 5.4, GUI:
  * the connection's default mode and allowed agents;
  * the chat's Other agents mode;
  * approval cards;
  * the inbox in the chat, and messages put in front of the next turn;
  * delegated and message conversations as records;
  * passing the policy and grants with each turn.

  Until then nothing in the app delegates or messages.
* 5.5, the user's live check.
