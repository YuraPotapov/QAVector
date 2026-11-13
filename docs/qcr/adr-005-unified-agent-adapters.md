# ADR-005: provider contracts, Codex and agent tasks

Date: 2026-11-13. Implementation on `codex/phase-4-unified-agent-adapter`.
Checkpoint before Phase 4: `0bd6250` on `agents-delegation`.

## Scope and ownership

Phase 4 extends the existing CLI conversation runner. It does not replace chat
storage, the QCR Engine, the journal, native restrictions or either provider's
own session store. The user requested this implementation on an isolated branch.

* `integrations/agents/contracts.py` defines provider capabilities and distinct
  ModelRef, ConnectionRef, SessionRef, AgentProfile and AgentTask contracts.
* Each adapter owns its command line, authentication inspection, model list,
  event decoding, modes and mode descriptions. Supported features must be
  declared explicitly; boolean capability defaults are false.
* The runner owns processes, cancellation, raw logs and conversation storage.
  GUI reads provider metadata; no `kind == "codex"` or `kind == "claude"`
  behavior branches belong in shared UI or cost accounting.
* `qcr.adapters.agents.AgentRuntime` registers `agents.<id>.describe/execute`
  into an injected Registry/Engine. It owns neither a second engine nor a
  second task state machine. Task ID is durable in execution input; execution,
  session, connection, agent and provider-session IDs remain distinct.

## Codex transport (superseded by Phase 4.1, 2026-11-13)

**Decision (user, 2026-11-13): Codex turns run over `codex app-server`.** A
bridge process (`--codex-turn`, `integrations/agents/codex_turn.py`) is started
by the runner like any CLI: message on stdin, a JSON line per event on stdout.
It speaks JSON-RPC for exactly one turn: `initialize` (experimental API) ->
`config/read` -> `thread/start|resume` -> `turn/start` -> `turn/completed`.
The one-process-per-turn model is kept. Stdout carries every app-server message
verbatim plus the bridge's own `{"qavector": thread|decision|end}` lines.

* Modes: Read = read-only; Edit = workspace-write with `untrusted` approvals,
  file changes accepted, commands accepted only when Codex's own parse
  (`commandActions`) is all read/listFiles/search, others declined; Full =
  workspace-write, `never`. First live check (2026-11-14): declining every
  command left Codex unable to read its folder, since it reads only through
  the shell.
* The turn's QAVector server runs with `default_tools_approval_mode="approve"`.
  Under `untrusted`, Codex otherwise asks before each tool call
  (`mcpServer/elicitation/request`, `codex_approval_kind: mcp_tool_call`),
  and the bridge declined it in the same live check. The QCR host's grants
  decide what a tool may do. An approval asked for a tool of that server
  anyway is accepted; one for any other server is declined.
  Plan first = read-only + `collaborationMode: plan`; every turn states its
  collaboration mode, because the mode is sticky in a thread.
* Questions (`item/tool/requestUserInput`) interrupt the turn, then are answered
  empty. The record gets `questions`, the session waits for answers. A plan
  item becomes `plan`. Every other server request is declined or refused;
  nothing waits for a person during a turn. Approvals go to `user`, never auto-review.
* Selected skills: copied per turn and passed with `skills/extraRoots/set`. This
  was checked on 0.167.0-alpha: it lasts only for that process and does not
  write the configuration.
* app-server has no `--ignore-user-config`; `-c` merges into the user's tables.
  So the bridge reads `config/read` for the folder. When the user's MCP servers
  are present, it restarts with `mcp_servers.<name>.enabled=false` for each.
  A name that cannot be addressed fails the turn. Plugins, apps,
  computer/browser use, image generation, hooks and memories are switched off
  through `features.*`. A blank model now means the model named in the user's
  Codex configuration; before, it meant Codex's built-in default.
* Stop: SIGTERM -> `turn/interrupt` -> 3 s grace -> exit 130. The app-server
  has a session of its own, so the runner's group signal does not kill it
  mid-write. It still ends with the bridge when its stdin closes.
* Usage: per-turn tokens = the thread total at the end minus the total before
  the first request of the turn. Context occupancy = the last request's input
  against `modelContextWindow`.
* The runner now builds the command line before it records the turn. An
  adapter refusing a combination (`Unsupported`) refuses the turn instead of
  leaving one that never ran.
* `QAVECTOR_CODEX_TRANSPORT=exec` keeps the earlier transport below as a
  fallback until the user's live check; it is then to be removed.

### Earlier transport (exec, kept as a fallback)

Use `codex exec --json` with stdin for a turn and `exec resume <thread-id> -`
for continuation. This matches QAVector's existing one-process-per-turn model.
The first `thread.started` assigns the provider session ID; persist it immediately,
so an interrupted QAVector runner does not forget the already-created thread.
Never resume `--last`, silently retry a failed turn or transfer a Claude session
to another provider.

* Read uses the read-only sandbox. Full uses workspace-write, never
  danger-full-access. Native QAVector folder restrictions remain in force.
* Claude's Edit-without-shell cannot be promised by this Codex transport; it is
  unsupported, as are structured Plan first and selected library skills in v1.
  UI disables unavailable choices; direct requests receive `unsupported`.
* Set approval policy to never: unattended CLI calls must not hang on a prompt.
  Interactive approvals/fork/native image input are not advertised.
* Ignore the user config for execution and explicitly supply the MCP table.
  Blank model uses the Codex default, not the user's custom config model.
  Authentication stays owned by Codex; QAVector never copies auth files.
* The per-turn QCR server is required when supplied. Only socket/token-file
  paths are in argv. Existing GUI grants/token lifetime apply to either provider.
* `login status` output is sanitized: API-key fragments are never forwarded.
* Model discovery uses app-server initialize + model/list without starting a
  thread or sending a prompt. Older unsupported CLIs fail visibly.

The installed CLI used for argument/schema validation was
`codex-cli 0.167.0-alpha.3.2`. This is a tested version, not a claim that all older
versions support the required flags.

## Usage and events

Keep raw JSONL in the existing turn's stdout.log. The Task event envelope maps
chat events to `agent.started`, `message.delta`, `tool.started/completed`,
`agent.completed/failed/canceled`; usage is a separate `usage.reported` event.
An already-refused tool is not fabricated into a pending approval request.

Codex input tokens include cache reads: normalized uncached input subtracts
cached input, so adding input and cache_read does not count the same tokens twice.
Turn totals are not presented as context occupancy. Missing price is null,
including the session total, not zero. Claude's cumulative session-price
semantics and older stored chat records are preserved. This is instrumentation,
not the full cost collector/budget subsystem of plan §22.

## Agent Tasks and permissions

An owner binds a logical profile to a connection, project, absolute workspace,
mode, model, effort and restrictions. Requests cannot widen those settings.
Resuming a task session checks its connection, provider, workspace, owner profile
reference and restrictions. Existing unrelated chats are not silently adopted.

The host accepts `agent_profiles` only on its trusted owner pipe. Configuration
does not execute a model. Agent socket clients cannot configure profiles or
grant permissions. `agents.read` and `agents.execute` are explicit scoped grants;
no existing connection gains them automatically. Child tasks inherit no QCR
token; delegation and narrowing its permissions remain Phase 5.

Task invocation returns an ordinary execution handle. The Engine owns ACL,
exclusive execution, idempotency, deadlines, cancel, persisted results/events
and crash reconciliation. The adapter delegates work to `runner.send`, propagates
cancel to its stop Event and links session/raw-log artifacts. On recovery an
unfinished execution becomes unknown; it is never automatically resubmitted.

V1 executes one turn per task. Several tasks may continue one provider session.
Multi-session tasks, messaging and handoff are later Phase 5 work. Profile editing
and a shared task board in the GUI belong to Phase 7; the owner API is available
now, and existing chats remain ordinary chats.

## Sources and rollback

Official protocol references:
[non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode),
[app-server model/list](https://learn.chatgpt.com/docs/app-server).
Local `exec --help`, `exec resume --help` and generated app-server JSON Schema
were checked without executing a model.

No data migration is run. Existing connection/chat JSON keeps its format with
additive turn metadata. To use the pre-Phase-4 implementation, check out the
checkpoint on `agents-delegation`; do not reset/delete current user data.
Older code cannot run Codex connections, but their records are not rewritten
into Claude records.
