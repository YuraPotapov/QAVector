# QCR — Phase 4 implementation

Date: 2026-11-13. Base checkpoint: `0bd6250`.
Branch: `codex/phase-4-unified-agent-adapter`.
Design and limits: [ADR-005](adr-005-unified-agent-adapters.md).

## Implemented

* Codex is available in agent connection discovery and the existing GUI/CLI
  conversation flow: login inspection, model catalogue, new/resumed turns,
  normalized events, cancellation, usage and per-turn QCR MCP configuration.
* Common provider capabilities, identity contracts and an explicit unsupported
  outcome. GUI modes, explanations and model-default labels come from metadata.
  A test uses an invented third provider to verify this independence.
* Provider IDs are persisted at startup. Connection/provider mismatches are
  rejected. Existing Claude conversations and cumulative price accounting remain
  compatible. Missing provider prices remain unknown.
* Agent task lifecycle runs on the existing Engine/Registry/journal. Owner-bound
  profiles, scoped discovery/invoke, idempotency, failure, cancel, timeout,
  session continuation, shared events and raw artifacts are covered by tests for
  both providers. There is no second CLI process runner.
* Trusted host protocol: `agent_profiles` configures the runtime; ordinary
  `capabilities/describe/invoke/get/wait/events/cancel` calls use the same facade.

## Validation

Final regression run: **525 passed** (all agent integration tests, QCR,
journal and tracked packaging checks; 49.51 s). GUI: **145 passed** (agent chat,
connections, model picker and third-provider metadata; 160.05 s). No failures,
errors or skips.

The Codex CLI fixture starts the real MCP server using the adapter's generated
configuration and connects to the real host/socket: granted reads are visible,
ungranted actions are absent, and revoking the token refuses the next turn.

Installed Codex CLI accepted both generated new-turn/resume argument lists with
`--help`; app-server JSON Schema generation succeeded. These checks send no prompt.

No real Claude/Codex model turn, frozen build or complete repository-wide suite
was run in this implementation task. The full Phase 4 acceptance gate remains
pending a real Codex chat and allowed QCR operation in the user's installation.
The automated CLI fixtures do not establish live account/model availability.

## Trying the implementation

In Integrations → Agents, add a **Codex** connection. The program can be found
on PATH or selected explicitly. Check sign-in, choose a model from its catalogue
or leave the Codex default, then start a chat in Read or Full. Enable the existing
QAVector access switches to give that connection specific QCR operations.

Edit-without-shell, Plan first and selected library skills are unavailable for
Codex v1; the adapter refuses these explicitly. Existing Claude features remain.

For embedding the Task API, construct `AgentRuntime(registry, engine, project_id,
sessions_root, agents_path)`, configure `AgentProfile` objects, and call
`submit(caller, agent_id, message, session_id=..., idempotency_key=...)`.
Use the existing Engine to wait/cancel/read events; `task(caller, execution_id)`
returns the task identity with the execution. Host owners can instead send:

```json
{
  "type": "agent_profiles",
  "id": 10,
  "sessions_root": "/absolute/project/agent-sessions",
  "agents_path": "/absolute/agents.json",
  "profiles": [{
    "id": "developer",
    "name": "Developer",
    "connection_id": "codex",
    "project_id": "project-id",
    "folder": "/absolute/workspace",
    "mode": "read"
  }]
}
```

This registers capabilities; it does not issue `agents.execute` or start work.
An authorized invocation supplies `task_id` and `message` to
`agents.developer.execute`. The Python facade generates task IDs itself.

L.2/L.3/L.4, Jira writes, Teams, cross-provider delegation, the Workbench and the
full usage/budget subsystem were not changed by this phase.
