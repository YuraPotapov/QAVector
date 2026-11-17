# ADR-007: Jira writes through QCR, confirmed by a person (plan §9.5.2)

Date: 2026-11-17. Status: **Accepted** (user, 2026-11-17: the plan in the
2026-11-17 sync - comment and transition first, one confirmation card, a
separate "Jira writes" switch, comment deletion for people only).
7.1 (core), 7.2 (GUI) and 7.3 (the other writes) implemented 2026-11-17 -
[report](jira-writes-report.md); the user's live check is open.
Builds on [ADR-006](adr-006-agent-messaging-and-delegation.md) (how an agent
reaches QCR, the Approve card) and Phase 1B (provider operation tables).

## What exists (audit)

* `integrations/jira/capabilities.py` declares four reads (`issue`,
  `comments`, `search`, `statuses`). `integrations.reader.ProjectReader`
  runs them in the QCR host with the project's credentials, checks input and
  output against the contract, redacts the token, the basic-auth form, the
  gateway address and webhooks, and turns every failure into a value-free
  code (`READ_ERRORS`).
* `qcr.adapters.integrations.IntegrationAdapter` registers one resource per
  connection (`integrations.<provider>.<connection>`) and one read capability
  per read contract. Nothing is invocable.
* The Integrations page writes through `--integrations call <connection> <op>`,
  that is `integrations.cli._call` -> `operations.call` -> `writes.<op>`.
  Every write answers with the issue read back afterwards. `writes.Refused`
  says why a write was not made (`fields`, `conflict`, `invalid`). A change
  made or failed is a line in `integrations-activity.jsonl`, without its text;
  `activity.record` already takes a `source`, but nothing passes one.
* An agent's actor is `agent:<connection>/<session>`. Its grants come from the
  connection's "QAVector access" switches (`integrations.agents.store.ACCESS_GRANTS`,
  mirrored in `gui/cms_gui/qcrhost.py`). The Approve card for a delegation
  is `agent_approval` -> `ChatTab.agent_request` -> `answer_agent`.

## Decisions

**D1. The provider declares its writes; the generic adapter registers them.**
`OperationContract(access="write")` entries are added for `comment` and
`transition` (then `assign`, `edit`, `attach`, `create`, `comment_edit`).
`comment_delete` is not declared: deleting is for people only (§9.5.2,
Critical). The `Provider` gains two optional callables, both Jira's own code:

* `prepare(name, connection, token, request, get)` - before anything is shown,
  reads what the person needs to decide (scope-checked like a read) and returns
  `{request, card, editable, noop}`: the request made exact (a status name
  resolved to the transition id), what the card shows (issue key and summary,
  "from -> to", the comment text), which input fields the person may change,
  and `noop` when the issue is already where it should be;
* `write(name, connection, token, request, get)` - the existing `writes.<op>`.

`integrations` still knows nothing of QCR, agents or approvals ("base knows
no consumers").

**D2. The host carries the write out, with the code the page uses.** The
reads already run in the host with the project's credentials, so the writes
do too, through `ProjectReader.prepare` / `ProjectReader.write`. These use the
same `operations.call`/`writes` functions as the page and the same redaction.
The token never reaches the agent, the card, the journal or the activity log.
This deviates from the 2026-11-17 plan, which said the GUI executes. The
constraint behind that wording - the token stays in QAVector, not with the
agent - holds either way. Executing in the host avoids a second route through
the GUI process.

**D3. One write = one execution that waits for a person.**
`integrations.<provider>.<connection>.<op>` is `long_running`, `supports_cancel`
(until approved), `supports_idempotency`, risk `high`, permission
`integrations.<provider>.<op>`; the resource permission is
`integrations.<provider>.write`. The invoker:

1. `requirements`: nothing to add (scope is checked in `prepare`);
2. `start`: off the host's lock, `prepare` runs; a failure fails the execution
   with its code; `noop` succeeds at once with `changed: false` and the issue
   as it is; otherwise the execution is `dispatched` with progress
   `approval.requested` (the card), and the GUI is told
   `{"type": "integration_approval", execution_id, actor, card, editable}`;
3. the person answers (`integration_answer`): **deny** fails it with
   `denied`; **approve** may bring the editable fields changed. The approved
   version is validated against the input schema again, and that version is
   what is written. The `approval.answered` event says who and whether it was
   edited (not the text);
4. the write runs in a worker thread; the execution succeeds with
   `{changed: true, summary, issue}` (the issue after the change) or fails
   with a code.

The Engine allows one unfinished execution per resource, so a connection has
at most one write waiting or running. A second write meanwhile is `busy`;
the agent waits for the first one. Writes are never retried; the same
idempotency key returns the execution already made.

**D4. Errors say what an agent can act on, nothing more.** The read codes,
plus: `denied` (the person said no), `fields_required` (the transition's
screen asks for fields - their names in the message, `needs` in the output),
`conflict`, `refused` (a `writes.Refused` sentence, which is ours, built from
transition and field names), `not_offered` (the issue offers no such
transition - the ones it offers are named), and `unknown_outcome`: the
service could not be reached **after** the request was sent, so whether the
change was made is not known; the agent is told to read the issue before
anything else. No Jira response text reaches the agent.

**D5. One activity log.** Each write, made or failed in Jira, is a line in
`integrations-activity.jsonl` with `source = {label: "Agent <connection name>",
detail: <execution id>}`. As before, the line has no comment text and no
field values. A denied write, and one refused before Jira changed anything
(`fields_required`, `conflict`, `refused`, `not_offered`), is not logged - as
for the page's own writes; the journal has it.

**D6. A separate switch.** "QAVector access" gets `jira_write` ("Change Jira:
comment, move issues - you approve each"), off by default and for existing
connections. It grants `integrations.jira.write` (the `~writes` resource) plus
one permission per declared write. Reading Jira stays its own switch. A
change's result carries the issue as it is afterwards, so a connection allowed
to change Jira sees the issues it changes.

**D7. The person confirms in one place, seen from two.** The card is shown in
the agent's conversation (in its feed, where it happened) and in a
"Waiting for confirmation" list on the Integrations page. Both are views of
the same execution: answering in one closes the other. A comment's text can
be changed before Approve; a transition's target cannot (deny and let the
agent ask again).

## Known limits

* A cancel or the deadline that lands while an approved write is in flight
  ends the execution, but the write may still be made; the activity log
  records what happened. The flight is one Jira request plus the read-back.
* Pending confirmations live in the host, so they end with QAVector
  (`source_unavailable`), as a delegation waiting for Approve does.
* Agents attach no files and post plain text (no editor document, no
  mentions on Cloud) in this version.

## Increments

* **7.1 core**: contracts, `prepare`/`write`, `ProjectReader.prepare/write`,
  the adapter's write capabilities and invoker, host `integration_answer`,
  activity source; the access switch in the store. Tests with a fake Jira.
* **7.2 GUI**: the card in the conversation (comment text editable), the
  Integrations page list, the switch in the connection dialog. Light and dark
  screenshots.
* **7.3**: `assign`, `edit`, `comment_edit`, `create`, `attach` on the same path.
  The card became the provider's to describe: `{title, changes: [{label, from,
  to}], notes, edit: {field: {label, value, was, multiline, required}}}`, with
  dotted names for nested request fields (`fields.summary`). A contract names
  its local-file inputs (`local_paths`); QCR takes such a file only from inside
  the asking conversation's folder, which the GUI sends with the turn's actor
  (`folder`), and fails with `out_of_folder` otherwise. `edit` and
  `comment_edit` carry the `updated` time read for the card, so a change made
  in Jira in between is a `conflict`, not overwritten. `create` is allowed only
  in a project that the connection's predefined query reaches.
* The user's live check: an agent comments on an issue in a test project, and
  moves it, with Approve and Deny.
