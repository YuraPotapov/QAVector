# ADR-010: agent roles for delegation

Date: 2026-11-17. Status: **Accepted** (user, 2026-11-17: D2 roles for every
conversation with Other agents on; D4 no verdict counts as rework; D5 the
templates as proposed; D3 the roles are told to the agent in its session). Builds on
[ADR-006](adr-006-agent-messaging-and-delegation.md) D9a (model and effort per
task) and [ADR-009](adr-009-waking-the-delegator.md) (`after`, waking the
delegator).

## What the user asked

The user keeps a list of roles in settings. Each says what it does, and has a
default model and effort. The roles are available to every chat, so a
delegating agent can use the role the user names: "give **dev** the code work,
**tester** runs the tests and finds possible problems, **reviewer** (a strong
model) reviews and approves or sends it back for rework". Without any roles,
the agent must not be helpless.

## What exists

* `agents.json` (app-wide) holds the agent connections. A connection is a
  delegation target, `agents.<connection>.delegate`.
* A task may say its `model` and `effort`; `agents.<id>.describe` lists what
  the target offers and what it is **Good at** (`role` on the connection).
* Tasks have names and `after`. The delegator says when to be woken, and is
  woken with each task's state and answer (ADR-009).

## Decisions

**D1. What a role is** (stored in `agents.json` beside `connections`, as
`roles`; app-wide, *decided*; per project later):

| Field | Meaning |
|---|---|
| `id` | lowercase, fixed once made (`dev`, `tester`, `reviewer`); what an agent and `after` name |
| `name` | what a person reads |
| `does` | what it does: the description the delegating agent chooses by (up to 500 characters) |
| `connection` | the agent program it runs as |
| `model`, `effort` | its defaults; empty is the connection's own |
| `mode` | the most it may do: Read, Edit or Full. A task never gets more than this, nor more than the delegating conversation has (ADR-006 D2) |
| `instructions` | added to each of its tasks: how this role works (up to 4,000 characters) |
| `verdict` | whether it ends with a verdict, `approve` or `rework` (D4) |

**D2. Roles are delegation targets.** Each role is offered as
`agents.roles.<role>.delegate`, with its own `describe`, to every conversation
whose Other agents mode is not Deny. This holds whatever its connection's
**Allowed agents** list says: the person made the role in order to use it from
any chat. A task given to a role runs on the role's connection, with:

* its model and effort - the agent may change them for a task, and the card
  says so;
* the lowest of three modes: the role's, the conversation's and the task's;
* its instructions before the task, under "Your role: <name>".

The child conversation records which role it ran as (`source.role`), for the
cards and for Usage. Connections stay targets as they are, so with no roles
nothing changes (*decided: the agent is never helpless*).

*Confirmed by the user, 2026-11-17:* the cap is on **access** (Read / Edit /
Full), not on the model. A cheap delegating agent - Sonnet, low effort - may
hand a review to Opus at max effort. Only the access a child gets is never more
than the conversation that hands it work. Letting a task go above that with
the person's approval was offered and not taken.

**D3. The agent is told about the roles - in its session** (user, 2026-11-17:
"the list of roles must reach the session, so that the agent sees what it
can do when Other agents is on"). It works like this:

* The runner puts the list in front of the turn's prompt: each role's id,
  name, program, model, effort, mode and what it does, and the connections it
  may use directly.
* This happens only when the conversation's Other agents mode is not Deny and
  the turn has QAVector access.
* It happens on the conversation's first such turn, and again whenever the
  list changed (the session keeps its fingerprint). The conversation remembers
  it between turns.
* What the person wrote is recorded as they wrote it. The list is in the turn's
  prompt file, not in the chat.

As well as that, `qavector_list_capabilities` lists
each role with its description. The MCP text says: when the person names a
role, use it; otherwise choose a role by its description before a bare
connection. `agents.roles.<role>.describe` gives the full description,
instructions, mode, verdict, and the models and efforts of its program.

**D4. A verdict decides what comes next** (*decided*). A role with `verdict`
is told to end its answer with one line of JSON:
`{"verdict": "approve" | "rework", "issues": ["..."]}`. QAVector reads the last
such line.

* `approve` - the task succeeded, and what is `after` it starts.
* `rework` - the task itself ran fine, but its outcome is `rework`. What is
  `after` it is skipped, and the delegator is woken (as on a failure, ADR-009)
  with the issues.
* No verdict line - the outcome is `no_verdict`, treated as `rework`, with the
  answer as it is. A reviewer that did not decide does not let a commit
  through.

The rework loop dev -> review -> rework -> dev is bounded by ADR-009 D6:
3 failed rounds, then the person is asked.

**D5. Templates** (*decided*). **Add the usual roles** fills in three, which
the person then changes:

* **dev** - "Changes code to do the task; runs what it changed." Mode Edit.
  Its connection is Codex when there is one, else the first connection.
* **tester** - "Runs the tests and the relevant scenarios; reports failures
  and likely problems with evidence; changes nothing." Mode Read.
* **reviewer** - "Reviews the change and its evidence; approves it or sends it
  back with concrete issues." Mode Read; verdict on. Its connection is a Claude
  one when there is one; its effort is high.

Template models stay empty (the connection's own) until the person picks
them, and nothing is guessed about which models exist.

**D6. Settings.** Integrations -> Agents gets a **Roles** tab:

* a table in the project recipe (Name, Does, Agent, Model, Effort, Mode,
  Verdict) with Add / Edit / Remove;
* its empty state offers **Add the usual roles**;
* the role form is read-only until Edit (inline-edit rule) and checks that its
  connection exists;
* a role whose connection was removed is shown as broken and is not offered to
  agents.

**D7. Cards and Usage.** A task card says "TASK FOR REVIEWER (Claude)", and its
result says the verdict and the issues. Usage gets a "Role" breakdown (from
`source.role`).

## Done 2026-11-17 (uncommitted)

R.1 and R.2:

* **Core:**
  * `integrations/agents/roles.py` - kept in `agent-roles.json` beside
    `agents.json`, not inside it, because `store.save` rewrites that file with
    connections only;
  * the `agents roles / role-save / role-delete` commands;
  * roles on the `agent-roles` resource in the delegation adapter, the verdict
    and its `rework` outcome;
  * the runner's session preface;
  * the role in the child's `source`.
* **GUI:** the Roles tab (`gui/cms_gui/pages/agentroles.py`, read-only until
  Edit), the role on task cards and in wake-up messages, and the
  `agent-roles` grant.
* **Tests:** `tests/test_qcr_roles.py` and the Roles tests in
  `gui/tests/test_agents_page.py`. Light and dark screenshots were checked.

R.3 (Usage by role) is done too: `usage.summarize(by=("role",), role=...)`,
`analytics.usage.summary` takes `role`, and Agents -> Usage has a Role
breakdown.

## Increments

* **R.1** core:
  * roles in the store;
  * `agents.roles.<role>.delegate` / `.describe`;
  * mode, model and effort resolution;
  * the instructions in the task;
  * the verdict parse and its outcome in `after` and wake-ups;
  * GUI grants;
  * MCP text;
  * tests with fake agents.
* **R.2** GUI: the Roles tab and form, the templates, role names on cards and
  in wake-up messages; light and dark screenshots.
* **R.3** Usage by role.
* The user's live check: dev -> tester -> reviewer -> commit.

All roles share one resource, `agent-roles` (a grant does not change when
the list does); they are not exclusive. A task for a role is limited by its
connection's two at a time (ADR-009).
