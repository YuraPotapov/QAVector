# QCR - Jira writes confirmed by a person (ADR-007 7.1-7.3)

Date: 2026-11-17. Design: [ADR-007](adr-007-jira-writes.md). Closes the code
part of the plan's Phase 2 item "integration writes through the Execution
Engine" for Jira: `comment`, `transition`, `assign`, `edit`, `comment_edit`,
`create`, `attach`. The user's live check is open.

## What changed

Core:

* `integrations/contracts.py`: `Provider.prepare` / `Provider.write`,
  `WRITE_ERRORS`, `WriteError`. A sentence passes only for codes of the
  provider's own making (`fields_required`, `conflict`, `refused`,
  `not_offered`).
* `integrations/jira/capabilities.py`: `comment` and `transition` write
  contracts; `prepare` (scope check, issue read, card, transition resolved by
  id / target status / name, no-op when already there); `write` (the page's
  `writes.<op>`, `Refused` -> codes, status 0 -> `unknown_outcome`).
  `comment_delete` is not declared.
* `integrations/reader.py`: `ProjectReader.prepare` / `.write` - contract
  checks, credentials, redaction as for reads; one activity line per change
  made or failed in Jira, with the caller's `source`.
* `qcr/adapters/integrations.py`: a `~writes` resource per connection
  (permission `integrations.<p>.write`), long-running, cancelable,
  idempotent-keyed capabilities; `WriteInvoker` holds the prepared change until
  `answer`, writes the approved version off the host's lock.
* `qcr/host.py`: `integration_approval` (host -> GUI), `integration_answer`
  (GUI -> host), activity label `Agent <connection name>`, pending changes
  ended on close. `qcr/mcp_server.py`: the tool texts mention Jira changes.
* `integrations/agents/store.py`: the `jira_write` switch.

GUI:

* `qcrhost.py`: `integration_changes` (what waits, how it ended),
  `answer_integration`, `set_integration_draft`, `dismiss_integration`; a
  core that stops ends what waited (`failed`, or `unknown` if it was sent).
* `agentcards.IntegrationChangeCard`: issue, connection, from/to for a move,
  the comment editable while it waits; Approve / Deny / Dismiss.
* The conversation shows its own changes as cards in its feed, where each
  happened (by time among its events); Jira gets an **Agent
  changes** tab (added by the Integrations page through `JiraView.add_tab`),
  with every conversation's changes and the selected one's card. Its title
  counts what waits.
* Connection dialog: "Change Jira: comment, move issues - you approve each".

7.3 (same day):

* The card is the provider's: `title`, `changes` (from - to), `notes`, `edit`
  (text fields the person may change, `was` for the old value, `required`).
  Dotted names reach nested request fields (`fields.summary`).
* `assign` looks a name or email up among the people the issue may be
  assigned to, and names everybody when several match. `edit` checks the
  issue's edit screen and offered priorities. `comment_edit` reads the
  comment. Both carry the `updated` time from the card, so a change made in Jira
  in between is a `conflict`. `create` resolves the type and choice fields by
  name, asks for the screen's required fields before any card is shown, and is
  allowed only in a project that the predefined query reaches. `attach` takes
  a file only from the asking conversation's folder (`OperationContract.local_paths`,
  the actor's `folder` from the GUI, `out_of_folder`).
* Activity names the new issue's key for `create`.

## Tests

* `tests/test_qcr_integration_writes.py` (25; 11 for 7.3): the real store, reader, Registry,
  Engine and journal against a loopback Jira - nothing sent before Approve,
  the edited text is what is posted and the key cannot be edited, deny sends
  and logs nothing, transition by status name, no-op, not offered, fields
  asked for (not logged, as for the page), `unknown_outcome`, Jira's words not
  passed on, scope, busy + idempotency, cancel/close, separate grants, no
  delete, the token in no card, output, event or log line; for 7.3: assign by
  name and several matches, edit with a changed summary / no-op / closed field
  / conflict, comment edit, create with required fields / names resolved /
  out of scope, attach in and out of the folder.
* `gui/tests/test_integration_changes.py` (8): through `QcrHost._take` as the
  pipe delivers - card in its own conversation only, edited draft sent, empty
  comment refused, denial, a change that no longer waits, core stopped, the
  Agent changes tab and its count, agent named by its connection, an edit card
  with a nested field and a required one left empty.
* Run and passing: core `test_qcr_integrations`, `test_qcr_host`,
  `test_qcr_mcp_server`, `test_integrations_jira`, `test_qcr_agent_access`,
  `test_qcr_registry`, `test_plugin_contracts`, `test_qcr_codex_access`,
  `test_qcr_delegation`, `test_integrations_agents_store`,
  `test_integrations_store`, `test_integrations_agents_cli` (297 + 69); GUI
  `test_agents_page`, `test_qcr_agent_grant`, `test_agents_others`,
  `test_agentchat`, `test_delegation`, `test_integrations_page`,
  `test_integrationhost`, `test_qcr_route`, `test_qcr_services`,
  `test_qcr_approvals_gui`, `test_theme` (64 + 123 + 182).
* Light and dark screenshots of the chat cards (comment, move, edit, create,
  attach) and the tab were checked.

Not run: the full repository suites, `test_main_window.py`, a frozen build, a
real Jira, a real agent turn.

## Live check (the user)

In a test Jira project: turn on **Change Jira** for an agent connection; ask
the agent to comment on an issue and to move it to another status. Check the
cards in the chat and on Jira -> Agent changes, edit the comment before
Approve, Deny the other once, then ask again and Approve. Activity should
name the agent; the issue in Jira should hold the edited text.
