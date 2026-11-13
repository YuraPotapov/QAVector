# QCR — Phase 5.1: delegation core

Date: 2026-11-13. Branch: `codex/phase-4-unified-agent-adapter`.
Design: [ADR-006](adr-006-agent-messaging-and-delegation.md) (accepted the same day).

## Implemented

* **Data.** An agent connection has `others_default` (auto / approve / deny)
  and `allowed_agents` (other connection ids, never itself). New connections
  get approve, with every existing connection allowed. Older ones read as deny,
  with none allowed. A conversation has `others`, taken from its connection
  unless given; older ones read as deny. `session-new` / `session-set` accept
  `others`.
* **Host.** `qcr/adapters/delegation.py` registers every connection as a
  target (`agents.<id>.delegate`, resource `agent-connection~<id>`) plus
  `agents.tasks.verify`. Owner-pipe messages:
  * `agent_connections`: where the connections and conversations are;
  * `actor` with an `agents` policy: the delegating conversation's session,
    connection, folder, mode, restrictions and mode for other agents;
  * `agent_answer`: the person's approve or deny.

  None of them is accepted from the agent socket. Which targets a turn sees is
  its grant (`agents.delegate` on each target's resource).
* **Approve** waits `dispatched` with `approval.requested`, and the host sends
  `agent_approval` to the GUI. Nothing runs until `agent_answer`. A denial is
  the result (`denied`), and cancel while waiting runs nothing. **Auto** runs
  at once. **Deny**, no policy, or the agent's own connection are refused
  before anything runs.
* **Child**: one turn of a new conversation of the target. It is a record
  (`source.record`, `ref = qcr-task:<execution>`) in the delegator's folder,
  under its restrictions, in the requested mode capped by the parent's (the
  parent's mode by default; a higher mode gets `mode_exceeds_parent`). It runs
  with no QAVector access, so it cannot delegate further (depth 1), and its
  first message is the rendered task (goal, criteria, summary), never a
  transcript.
* **Output** = evidence: the answer (≤ 20 000 chars), turn state, changes
  summary, artifacts, each criterion `unverified`, and `verified: false`.
  `agents.tasks.verify` records a verdict per criterion with an evidence
  execution. Only the delegator's own tasks and evidence count, and the
  evidence must have ended. The verify output gives the whole acceptance list
  and `verified`.
* **Limits**: at most 3 child tasks per conversation (`limit_exceeded`) and
  one running task per target (Engine exclusivity, `busy`). The default time
  is 30 minutes, the maximum 6 h. Closing QAVector fails the tasks still
  waiting for approval and stops the running ones.
* `AgentRuntime` and delegation share one turn runner (`run_turn`,
  `turn_artifacts`).

## Validation

* `tests/test_qcr_delegation.py`: **12 passed**. The real host runs
  in-process, with stand-in Claude and Codex CLIs. One end-to-end test drives
  Codex -> Claude through the real `--qcr-mcp` server and agent socket: the
  fake Codex turn calls `qavector_invoke agents.claude.delegate`, waits with
  `qavector_wait`, and returns the child's answer.
* Core selection (agents, QCR, journal, packaging, cycle agent connections and
  plugins): **832 passed, 1 skipped**. GUI QCR modules, each in its own
  process: **86 passed**.

Not run: a real model turn, the full repository suite, a frozen build.

## Not yet (later increments)

* 5.2: content-addressed context packages with file and artifact references,
  provenance and redaction.
* 5.3: messages.
* 5.4: GUI. Until then the GUI sends no `agent_connections` and no `agents`
  policy, so **no conversation can delegate yet**. Existing behaviour is
  unchanged.
* 5.5: the user's live Claude <-> Codex check.
