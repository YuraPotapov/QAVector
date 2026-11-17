# ADR-014: a Jira issue as the context of a conversation

Date: 2026-11-17. Status: **Accepted**. The user decided on 2026-11-17:

* agents may read the private notes; they are private from Jira, not from
  the agents working here;
* a conversation is linked to an issue when its agent reads the issue's
  details, never by a search.

Builds on [ADR-007](adr-007-jira-writes.md) (Jira through QCR),
[ADR-008](adr-008-usage-and-cost.md) (usage by issue) and
[ADR-012](adr-012-composition-workbench-history.md) (history).

## The problem

An agent in a plain chat answered "what is on me in To Do" and then "what are
the details of QA-142" through QCR: a search, then the issue and its
comments. The answers were right. But the conversation was not the issue's:

* **It was not linked to the issue.** Its `source.ref` was empty, so the
  issue's page in Jira did not list it, and the issue's usage did not count
  it. Only the Jira page's Delegate... sets a ref, and only at the start.
* **The agent saw only Jira.** What QAVector knows about the issue was out of
  its reach: other conversations about it, the tasks they handed on, what
  that cost, and the person's private notes.
* **The issue read is large.** It carries the description three times (HTML,
  text, and the editor's document), and each comment twice. The agent had to
  read its own spill file in parts.

## Decisions

**D1. A conversation may be about several issues.** What it was started for
stays its `source.ref`. Issues it was linked to since are its links. They are
kept beside its record (`links.json`), not in it, because a running turn
rewrites the record and must not drop a link made during that turn.
`sessions.refs_of()` is both. The issue's conversations (`listing(ref=)`)
and its usage (`usage.for_ref`) count the links too.

**D2. Reading an issue's details links the conversation.** When an agent's
read of `integrations.jira.<connection>.issue`, `.comments` or `.context`
succeeds, the host links the agent's conversation to
`jira:<connection>:<KEY>` and tells the GUI (`conversation_linked`). A search
links nothing, because it is not reading an issue's details.

**D3. One read with everything: `integrations.jira.<connection>.context
{key}`.** It returns:

* the issue's fields, its description as text (up to 6,000 characters,
  marked when cut), and its 5 newest comments as text (1,500 characters
  each), with how many more there are;
* from QAVector: the conversations about the issue (started for it, or
  linked), the tasks they handed on, their usage (own and inclusive), and the
  private notes.

Its permission is the issue read's (`integrations.jira.issue`). The MCP tool
text tells agents to start with it for one issue.

**D4. The person sees and corrects the links.** The chat's header lists the
issues a conversation is about, under ABOUT. A click opens the issue in Jira.
A link can be undone (`agents session-link`); what the conversation was
started for cannot.

## As built (2026-11-17, uncommitted)

* `integrations/agents/sessions.py`: `link`, `unlink`, `links_of` and
  `refs_of`; `load` adds the links, `save` leaves them out.
* `qcr/host.py`: `_linked` after an agent's read.
* `qcr/adapters/issue_context.py`: registered beside the Jira reads
  (`IntegrationAdapter.refreshed`).
* `integrations/agents/cli.py`: `session-link`.
* GUI: the ABOUT chips in the chat, `conversation_linked`, and opening the
  issue from the chip.
* Tests:
  * `tests/test_qcr_issue_context.py`;
  * the chips test in `gui/tests/test_agentchat.py`;
  * `test_qcr_agent_access` lists the context beside the issue read.

**After the user's first look (2026-11-17).**

* **The ABOUT chip did not open the issue.** The issue was outside the list's
  filters, so the page did nothing. `JiraView.open_key` now looks the issue up
  by its key first, then opens it as Ctrl+K opens what it found.
* **Haiku read `issue` rather than `context`, and hit the same wall.** Through
  QCR, `issue` and `comments` now give the page's HTML and editor documents
  empty: the schema keeps the fields, and the text beside them is whole.

## Later

* The Jira page's activity log (changes agents proposed) in the context.
* A search's rows trimmed the same way.
