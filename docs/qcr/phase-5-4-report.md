# QCR — Phase 5.4: other agents in the GUI

Date: 2026-11-14. Branch: `codex/phase-4-unified-agent-adapter`, on top of 5.3 (`12de8a1`).
Design: [ADR-006 D3, D4, D9](adr-006-agent-messaging-and-delegation.md).

## Implemented

* **The connection** (Integrations → Agents → Connections) has a section
  called "Other agents":
  * what its conversations start with: Approve each one, Auto, or Deny;
  * a checkbox for each *other* connection, with its provider. The
    connection itself is never listed.

  A new connection starts as Approve with every existing connection ticked.
  With Deny, the boxes are disabled. The dialog body now scrolls and the
  buttons stay at the bottom. Its height is capped at 88 % of the screen: with
  the new section the fields no longer fit a laptop screen.
* **The chat footer** has OTHERS: Auto / Approve / Deny.
  * A new conversation starts from its connection's default.
  * A conversation made before ADR-006 shows Deny.
  * A change in a begun conversation is saved (`session-set others`).
  * When the connection allows nobody, the buttons are disabled, and their
    tooltip says where to allow agents.
* **Each turn** (`QcrHost.grant_agent`) gets, besides its QAVector access:
  * `agents.delegate` and `agents.message` on every allowed target, plus
    `agents.delegate` on `agent-tasks`;
  * the conversation's policy (session, connection, folder, mode of this turn,
    mode for other agents, restrictions).

  Deny, or a connection that allows nobody, gets none of this. A turn now gets
  a token for other agents alone, even when no "QAVector access" switch is on.
* **The host** is told where connections and conversations are
  (`agent_connections`). This happens when it becomes ready, and again
  whenever the connections change.
* **Cards in the chat**, in a strip between the conversation and the composer:
  * **Task / message / question for <agent>.** With Approve it waits
    ("waits for you", Deny / Approve). It follows its execution: working, done,
    did not finish, denied. Then it shows the answer (and how many criteria
    are not verified yet), *Open conversation* (the child's record) and
    *Dismiss*. An Auto task gets its card from the execution itself, in the
    conversation whose actor made it.
  * **Message / reply from <agent>**: "goes with your next message". The next
    message the person sends starts with these messages, each marked as
    another agent's. Then they are acknowledged. *Dismiss* acknowledges one
    without passing it on.
* The cards follow the theme (`role="agentcard"`, see-through rows; a new rule
  makes their rows transparent). They were checked in light and dark
  screenshots.

Found and fixed on the way:
* The connection dialog's other-agents list showed "codex" in lower case.
  Provider names now come from the core's kinds.
* `PlanCard._quote` held a literal U+2029 that an editor's "remove unusual
  line terminators" had already deleted. It became `replace("", "\n")`, which
  would put a line break between every character. It is now the escape
  `"\u2029"`.

## Validation

* `gui/tests/test_agents_others.py`: **13 passed**. It covers grants and
  policy, Deny and nobody-allowed, storage and refresh, the dialog's defaults
  for new and existing connections, the footer's default and saved change,
  approve and deny, the task card following its execution, an Auto task and
  another conversation's task, and the inbox going with the next message,
  dismissed or not.
* Existing GUI files, each in its own process: test_agentchat 84, test_agents_page 54, test_qcr_agent_grant 10, test_transferdialogs 9, test_agent_provider_metadata 2, test_theme 16, test_window_fits_screen 3, test_qcr_route 25, test_qcr_inspector 15 - all passed. test_window_fits_screen first failed: the host's signals were wired where the pages are built, before the host exists; they are now wired after it.
* Core and QCR stay as committed in 5.3: no core files changed in 5.4.

Not run: the full GUI suite in one process (known to segfault on stale
deleteLaters, see the project notes), a real model turn.

## Next

5.5: the user's live check of Claude → Codex and Codex → Claude delegation, a
message without a task, and the Approve card.
