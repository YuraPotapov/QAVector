# ADR-015: Teams as an integration of its own (Phase 1T)

Date: 2026-11-17. Status: **Accepted in part** (user, 2026-11-17): a
connection holds default access for agents and cycles (D5). Teams has it
first; the other integrations are to be reworked the same way later. Builds on the plan's section 9.5.3 (user, 2026-11-12:
Teams is a separate integration, not only a cycle step) and on
[ADR-007](adr-007-jira-writes.md): an agent's change to Jira waits for the
person's Approve. This updates the 2026-11-12 plan with what has been built
since.

## What exists (audit)

* **The cycle step `teams.message`** (`cycle/plugins/teams.py`, 250 lines)
  does everything a post needs:
  * an Adaptive Card from Markdown (`card()`), with a title;
  * mentions from `Name <upn>` (`people()`);
  * HTTPS only (`_address_problem`, loopback for tests);
  * a 30 s timeout and a 20,000-character ceiling.

  Its webhook comes from a step field, usually a secret variable. It keeps no
  record of what it posted.
* **Integrations are built for Jira alone.**
  * `integrations.PROVIDERS = ("jira",)`.
  * The connection store's fields and checks are Jira's: site, flavour,
    auth, email.
  * The reader knows one provider.
* **ADR-007 gives writes a whole path.** Every part of it is
  provider-neutral:
  * `Provider.prepare` (what will change) and `write`;
  * the QCR write capability waits for the person (`integration_approval`);
  * the change card in the chat, editable before Approve;
  * Integrations -> Agent changes;
  * the activity log names who asked.
* **Teams mentions need the sign-in address (UPN),** not a git email. The
  cycle step had to learn aliases.

## Decisions (proposed)

**D1. A provider `teams`, one connection a channel or chat.** It has:

* a name, a fixed id, and what it posts to (channel, group chat or person,
  only as a label);
* the Workflows webhook URL. The URL is a **secret** in the project's secrets
  store, as a Jira token is. It is shown as host and mask only, and never
  appears in an answer, an error, the history or an event.

The connection store learns that each provider has its own fields and checks.
Jira's stay as they are.

**D2. Settings of a connection:**

* the default title and a footer (for example "- via QAVector");
* mention aliases (`name -> UPN`), offered as you type;
* the longest text, at most the step's 20,000 characters.

**D3. What can be done.**

| Operation | What | Who |
|---|---|---|
| `preview` | builds the card locally; nothing is sent | anyone with read |
| `history`, `post_of` | the posts QAVector sent through the connection | anyone with read |
| `post` | sends a post: title, Markdown text, mentions | the person directly; an agent through Approve |
| `test` | a real test post, from the connection's settings | the person only |

* `post` is an ADR-007 write: `prepare` shows the card as it will look, with
  the text editable on the change card. Approve sends it.
* A post cannot be taken back, so "Send again" makes a **new** post, and asks
  first.
* **202 is not "delivered".** A post's outcome is `accepted` (HTTP 202),
  `failed` (host and HTTP status), or `unknown` (no answer in time). It is
  never retried by itself.

**D4. The history of posts** is `integrations-teams-posts.jsonl`, kept apart
from the activity log because it keeps the content. Each post records:

* the title, text, mentions and outcome;
* who sent it: the person, an agent with its conversation, or a cycle step
  with its run;
* who approved it and when.

It never records the URL. It is trimmed like the activity log, and the
activity log gets a line without the text.

**D5. Default access on the connection** (user, 2026-11-17: "as for any
integration, there must be default rights for agents and cycles"). A Teams
connection has an **Access** section:

| Who | Levels | New connection |
|---|---|---|
| Agents | Off / Post with approval / Post | Post with approval |
| Cycles | Off / Post | Post |

* **The connection sets the ceiling.** An agent connection's own switch,
  **Post to Teams**, can only narrow it. With Agents Off, no agent sees the
  connection at all.
* **QCR checks it before anything runs,** where it checks the agent's switches
  today. A post with approval is an ADR-007 write: a card in the chat,
  Integrations -> Agent changes, and the Teams page's own list.
* The integration does not know what a cycle is: "agents" and "cycles" are the
  kinds of caller QCR tells apart (`agent:...`, `cycle:...`).
* **Jira keeps its present model** (the agent's Read Jira / Change Jira) until
  it is reworked the same way. Its `cycle_writes` setting was removed on
  2026-11-07 for being a setting about one kind of user; this section is the
  general form of it.
* Through ADR-014, a post made from a conversation about an issue says so.

**D6. The Teams page** on Integrations, beside Jira and Agents, embedded or
in its own window (`docs/integration-windows.md`). It has four tabs:

* **New post:** connection, title, Markdown text, mentions with aliases, the
  live card, and Send (which asks first, since a post cannot be taken back).
  The text left there is a draft.
* **Waiting:** the posts agents proposed, with Approve / Deny and the text
  editable.
* **History:** a table in the project's recipe, with every empty state
  designed. It shows a post and offers Send again.
* **Connections:** add a channel (name and webhook URL), check the address
  without sending, a separate Test post, and the settings. Read-only until
  Edit.

**D7. The cycle step.** The card, mention and send code moves to
`integrations/teams/`, and the step calls it. Its `webhook:` keeps working as
it is (tested). The step may name a `connection:` instead, and then its posts
are in the history. Nothing else in cycles changes. This is the one part that
touches cycles, whose future is open (see the user's 2026-11-17 note).

**D8. One post a minute a connection** (user, 2026-11-17: "a guard against
sending again - for now only one message a minute; it may get in the way
later, but for now we limit it"). The rule holds for everybody who sends:
the page, an agent and a cycle step.

* A post that may have reached the channel (`accepted` or `unknown`) blocks
  the next one for 60 seconds. A `failed` one does not count.
* The refusal says how long to wait (`retry_after`). It is not a post, so it
  is not in the history.
* The check and the sending happen under one lock per connection, so posts
  from different processes cannot slip through together.

**D9. No provider is named in the base** (user, 2026-11-17, on a hard-coded
`provider == "teams"`). Each provider has its own package:

* `integrations/<provider>/connection.py` holds the connection handling:
  `normalize`, `problems` and `credentials`, and optionally
  `secret_problem`, `keeps_token` and `account_changed`;
* `integrations/<provider>/capabilities.py` holds the reads and writes;
* `integrations/<provider>/page.py` holds the page's own operations.

The store, the reader and the CLI find these by the names in `PROVIDERS`.
Jira's connection code moved, unchanged, to `integrations/jira/connection.py`.

## Increments

* **T.1 core.** **Done 2026-11-17** (uncommitted):
  * `integrations/teams/`:
    * `webhook` (moved from the step, which now uses it);
    * `connection`, `history`, `capabilities` and `page`;
  * D9;
  * D8;
  * tests: `tests/test_integrations_teams.py`; the step's own tests still
    pass.

  As planned:
  * the provider (card, mentions, send moved from the step);
  * per-provider fields in the store and the secret URL;
  * the history;
  * CLI `--integrations` for teams;
  * tests with a loopback webhook.
* **T.2 QCR.** **Done 2026-11-18** (uncommitted):
  * `describe`, `preview`, `history` and `post_of` as reads; `post` as an
    ADR-007 write. `describe` was added on the user's word (2026-11-18): "give
    it the integration's attributes, it will work out what it needs". It gives
    everything set on the channel but its webhook, and where posting stands.
    Nothing tells the agent to read the history first, and the history is not
    filtered for it.
  * The agent switch **Post to Teams** (`teams` in QAVector access, off by
    default) grants the reads and the post.
  * D5 is held in two places:
    * when a caller is issued its grants (`QcrHost._issue` ->
      `IntegrationAdapter.narrow`), a grant over a provider whose connections
      set a ceiling becomes one grant per connection that is not Off for the
      caller's kind (`agent:` agents, `cycle:`/`run:` cycles; the person has
      no kind). A delegated task inherits the narrowed grants;
    * before each write (`WriteInvoker`), the connection's level is read
      again: Off refuses (`authorization_denied`), Auto writes with no card,
      Ask waits for the person.
  * The provider says what it allows through an optional
    `connection.access_of(entry, kind)`; `ProjectReader.access` and
    `ceilings` read it. Jira has none, so nothing changes for it.
  * Tests: `tests/test_qcr_teams.py` (13), through the real host too.
* **T.3 GUI.** **Done 2026-11-18** (uncommitted):
  * the Teams page (New post, History, Connections) from 2026-11-17, and its
    **Waiting** tab: `IntegrationChangesTab("teams")`, after New post. The
    change card names the provider from the capability, no longer "Jira";
  * the catalog's cards count waiting posts with Jira's changes;
  * the Connections form scrolls, so the page no longer holds the window
    taller than a laptop screen;
  * screenshots checked in light and dark.
* **T.4 the cycle step.** **Done 2026-11-18** (uncommitted). `connection:`
  posts through the channel, with its record, its one-a-minute rule and its
  Cycles access. `webhook:` works as before. A bug from 2026-11-17 is fixed:
  the step refused the channel the run handed it as an "unknown setting".
  A whole run is tested, and none of its files holds the webhook. Moving the
  existing cycles of claim/demo/helpdesk to `connection:` waits for the user.
* **D4's Activity line. Done 2026-11-18.** It was missing for the page's and
  the cycle step's posts; an agent's post was logged by the reader, as every
  QCR write is. Now every post is one line, without its words, and its
  summary is the outcome ("Post to Release: accepted (HTTP 202)"), not the
  title. Activity lines carry `provider`; a line without one is Jira's, and
  Jira's Activity tab shows Jira's lines only.
* **Gate (user):**
  * a person posts from the form;
  * an agent's post waits and is sent on Approve;
  * a cycle step posts through the old `webhook:` (and `connection:` if
    T.4 is done);
  * all of these are in the history, and the URL appears nowhere.

## For the user to decide

1. ~~Agents~~ - decided: default access on the connection, the agent's
   switch only narrows it (D5).
2. ~~The cycle step~~ - decided (user, 2026-11-17): existing cycles post
   through the integration, not with credentials in the Teams node (D7).
3. **Scope:** webhook posts only, with no reading of the channel and no Graph
   API, as decided on 2026-11-12. On 2026-11-18 the user asked for a
   **Limited** mark on the Teams card, so the page says it.
