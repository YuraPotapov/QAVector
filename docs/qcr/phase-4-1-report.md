# QCR — Phase 4.1: Codex over app-server

Date: 2026-11-13. Branch: `codex/phase-4-unified-agent-adapter`, on top of `9bd656a`.
Decision and design: [ADR-005, "Codex transport"](adr-005-unified-agent-adapters.md).

## Implemented

* `integrations/agents/codex_turn.py` is the bridge (`--codex-turn`). It runs one
  turn over `codex app-server` and is started by the unchanged runner like any
  CLI. The app-server's messages go to the turn's `stdout.log` as they arrive.
  The bridge adds `thread`/`decision`/`end` lines.
* The Codex adapter now offers **Read / Edit / Full**, **Plan first** and
  **selected library skills**. The GUI picks this up from the adapter metadata;
  no GUI code changed.
* Plan first: `collaborationMode: plan`. Codex's questions end the turn
  (`phase = questions`), and its plan item ends it waiting for approval
  (`phase = plan`). Every turn states its mode, so an approved plan runs in
  `default` mode in the same thread.
* Edit: workspace-write with `untrusted` approvals. File changes are accepted
  and commands are declined. A declined command is recorded as a refused tool
  call.
* Skills: copied per turn into the conversation and passed as an extra skills
  root of that process only.
* The user's Codex configuration is loaded because app-server has no
  `--ignore-user-config`. Its MCP servers are switched off by name, after a
  restart when there are any. Plugins, apps, computer/browser use, image
  generation, hooks and memories are off. Only the turn's QAVector server is
  present.
* Stop interrupts the turn (`turn/interrupt`) instead of killing Codex mid-write.
* Usage: the turn's own tokens, and real context occupancy (the last request's
  input against the model's window). Missing price stays unknown.
* Runner: the command line is built before the turn is recorded. A combination
  the adapter refuses now refuses the turn instead of leaving one that never ran.
* Fallback: `QAVECTOR_CODEX_TRANSPORT=exec` restores the earlier exec transport
  (Read/Full only). It is to be removed after the live check.

## Validation

* Core: **539 passed** (`tests/test_integrations_agents*.py tests/test_qcr_*.py
  tests/test_journal.py tests/test_packaging_tracked.py`). Codex adapter tests
  went from 15 to 29. They run against a fake app-server speaking the same
  JSON-RPC and cover: new and resumed threads, failure, crash, stop by
  interrupt, questions, plan then approved work, Edit declining a command and
  accepting a patch, Read declining a patch, skills root, switching off the
  user's MCP servers, a server name that cannot be switched off, and the exec
  fallback. The existing QCR/MCP test with the real host and socket now goes
  through app-server.
* GUI: **190 passed** (agent chat, agents page, turns, skills, model picker,
  provider metadata).
* Real `codex-cli 0.167.0-alpha.3.2`, **no thread and no model turn**: the
  bridge's start-up against this machine's configuration. The user's
  `node_repl` server was found, switched off and not started. All eight
  features read back as off, and the extra-root skill was listed. The root was
  not written to `~/.codex/config.toml`.

Not run: a real Codex model turn, a frozen build, the complete repository suite.

## Gate

Phase 4 stays open until the user checks it live: Read, Edit, Full, Plan first
(questions and plan), a selected skill and a granted QAVector capability on a
real Codex connection. After that check, the exec fallback is removed.

## Known limits

* `collaborationMode` and `request_user_input` are experimental app-server
  fields. If an older CLI rejects them, the turn fails visibly. QAVector does
  not fall back silently.
* In Edit, commands that Codex holds to be safe and read-only still run, and so
  do commands the user's own Codex rules allow.
* A blank model now means the model in the user's Codex configuration.
