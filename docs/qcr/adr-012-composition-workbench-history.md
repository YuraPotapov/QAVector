# ADR-012: composition, the agents' workbench, and history (Phase 7)

Date: 2026-11-17. Status: **D3-D5 accepted and being built; D1, D2, D6, D7
deferred** (user, 2026-11-17). The user is not sure cycles will stay in the
project for long: they are hard to maintain and to change. So composition is
not built on cycles now. The workbench and history (D4, D5, and D3 as far as
they need it) do not depend on cycles, and come first. Builds on ADR-003 (scenarios and cycles in QCR), ADR-006
(delegation), ADR-009 (waking), ADR-010 (roles) and ADR-011 (questions in the
same session).

## What Phase 7 asks (plan, Phase 7 and sections 11, 12, 14.2)

1. Composing capabilities into a workflow over the existing Cycle Engine, with
   conditions, outputs and a validated DSL.
2. An agent workbench: a shared task board and a timeline of the work.
3. History: tasks linked to their diffs, tests and artifacts; context reused.
4. Agents may propose a composition; it is published only after approval.

**Gate:** Odoo Auto Fix works end to end and shows its evidence, with no
unregulated recursion.

## What exists (audit)

**Composition is mostly there already, as cycles.**

* **The DSL.** A cycle file (`cycle/model.py`, `cycle/conditions.py`) has
  everything section 11 asks for:
  * steps with `needs` (ring and missing-step checks);
  * `if:` conditions on `${steps.<id>.status}` and
    `${steps.<id>.outputs.<name>}` (no `eval`);
  * `with:` inputs with references, and outputs;
  * `timeout`, `retry` with backoff, and `on_failure`;
  * phases, approvals and variables, validated at once (`problems()`).
* **The step plugins** cover the Auto Fix steps:
  * `service`, `scenario`;
  * `agent`, `agent_implement`, `agent_edit`, `agent_merge`;
  * `approval`, `check`;
  * `git_prepare_branch`, `git_commit`;
  * `jira`, `jira_transition`, `teams`, `report`.
* **A cycle is already a capability.** `cycles.<id>.execute` (ADR-003) takes
  variables and returns the run, its status and its steps.
  `cycles.<id>.pending` and `cycles.<id>.answer` cover its approvals.

**Gaps in composition.**

* **Agent steps run Claude only** (`cycle/connections.AGENT_KINDS =
  ("claude",)`). There is no Codex step, and no step names a role (ADR-010).
* **A cycle's steps do not run through QCR.** Their services, scenarios and
  agent turns are not executions of the run, so nothing links them.
* **Executions are not a tree.** `executions.parent_id` exists but nothing
  fills it. A delegated task does not name the execution it came from, and a
  cycle step does not name its run.

**The workbench and history are scattered.**

* **What there is:**
  * the chat feed (task, question, answer and wake cards per conversation);
  * Agents -> Activity (turns) and Usage;
  * the Jira issue's delegation tabs;
  * Log -> Executions (every QCR execution, flat).
* **What is missing:**
  * **No single place** shows every task handed out across conversations: what
    waits for approval or an answer, what runs, what failed. To find a task
    that asks, the person has to open the right conversation.
* **History pieces exist but stay apart.** A task's output keeps its diff
  (`changes`), artifacts, acceptance criteria and verdicts. ADR-011 chains its
  turns (`root`, `follow_up_of`). Nothing shows that chain as one timeline with
  its evidence.
* **Context reuse:** context packages are kept and content-addressed
  (`.qcr/context/`). Nothing offers to hand the same package on again.

**Agent-written compositions.** An agent with Edit access can write a cycle
file in the project. Once it is there, any agent whose connection has the
"cycles" switch can run it. Nothing tells a cycle the person wrote from one an
agent wrote.

## Decisions (proposed)

**D1. No new DSL: a workflow is a cycle.** Section 11's
`workflows.<id>.run` is `cycles.<id>.execute`. The cycle format already
validates `needs`, conditions, outputs, retries and timeouts. Two DSLs over one
engine would drift apart. What is missing is added to cycles (D2, D3).

**D2. Cycle agent steps use connections and roles.** An agent step may name a
connection of any supported kind (Claude, Codex) or a role (`role: reviewer`),
with model and effort. It runs through the same runner as the chat. A
reviewer's verdict becomes the step's outputs, so `if:` can branch on
`${steps.review.outputs.verdict} == 'rework'`.

**D3. Executions form a tree.** `parent_id` is filled in two places:

* a delegated task's parent is the execution of the turn that handed it on,
  or, when that is unknown, the conversation's root;
* a cycle run started through QCR is the parent of the QCR executions its
  steps start.

Log -> Executions and the workbench (D4) show the tree, which is what lets the
gate say there is "no unregulated recursion".

**D4. Workbench: an Agents -> Tasks tab.** One list of every task, question
and answer handed out in the project, across conversations, newest first:

* **columns:** state (waits for you / asks / running / done / failed /
  stopped), who handed it to whom (role, program), goal, questions so far,
  files changed, cost, when;
* **actions in place:** Approve / Deny, Answer, Stop, Open conversation,
  Open child;
* **filters:** state, connection, role, conversation; "Needs me" first (what
  waits for approval or a person's answer).

It is read from the journal, not from chats that happen to be open, so it is
complete after a restart.

**D5. A task's timeline.** Selecting a task shows its history in order:

* handed on, approved, started;
* each question and answer, and who answered;
* each verify verdict with its evidence, and the review verdict;
* files changed, with the diff (the existing diff view), and its artifacts;
* each turn's model, effort, tokens and cost.

**Reuse context** hands the same context package on again, to another
connection or role.

**D6. An agent-written cycle is published only after approval.**

* A cycle file that an agent created or changed is marked by its content hash.
  The core knows which files agents wrote: an agent's changes are recorded per
  turn.
* Until the person approves that exact content, QCR does not offer
  `cycles.<id>.execute` for it to agents. The person can still open, edit and
  run it.
* A change after approval needs approval again. The Cycles page shows
  **Proposed by an agent - Approve**.

**D7. The gate cycle.** An example cycle, `examples/auto-fix`, does the plan's
Odoo Auto Fix with plain cycle steps. The services and scenarios are
variables, so it is not tied to one app:

1. start the service;
2. run the scenario;
3. if it failed, a diagnose step (a role);
4. implement in a git worktree (a role, e.g. Codex);
5. rerun the scenario;
6. review (the reviewer role, with a verdict);
7. report.

The user runs it on their app as the gate.

## Increments

As decided (2026-11-17): **7.1 and 7.2 now; 7.3-7.5 wait** for the user's
decision on cycles. When composition comes back, two ways were weighed:

* an orchestrating agent following a saved "playbook" through the existing
  capabilities;
* a small workflow engine in QCR.

Neither is built on cycles.

**7.1 + 7.2 as built (2026-11-17, uncommitted):**

* **Core.**
  * `Delegation.board()`: one row a task, with its answers folded in. Its
    state is as the person would act on it: approval, waiting, running, asks,
    done, rework, skipped, denied, stopped, failed. `needs_person` is set when
    it waits for approval or asks.
  * `Delegation.history(task)`: the task's history, oldest first, plus the
    child conversation's own usage.
  * The owner messages are `tasks` and `task_history`. The journal lists by
    `capability_prefix`, so the board reads only agent executions.
* **GUI.**
  * Agents -> **Tasks** (after Chat). The table lists what waits for the
    person first, in the warning ink, with the filters All / Needs you /
    Active / Ended.
  * The picked task shows what it is and what it cost, its question with an
    Answer box, Approve / Deny, Stop, the two conversations, and its history
    with the asked and answered marks.
  * The board is read again when an agent execution changes, at most every
    400 ms.
* **D3, as far as tasks go:** the host fills `parent_id` for an answer to a
  task and for a verdict on it (`agents.tasks.reply`, `agents.tasks.verify`).
  It takes the task's id from the input, never the caller's word. Log ->
  Executions says "reply (part of 01234567)". A cycle run's steps wait with
  composition.
* **Not yet:** Reuse context.
* **Tests:** `tests/test_qcr_workbench.py` and
  `gui/tests/test_agent_tasks.py`. Light and dark screenshots.

* **7.1** Executions tree (D3) and Agents -> Tasks (D4), read-only first:
  list, filters, "Needs me", Open.
* **7.2** Actions on the board (Approve, Answer, Stop) and the task timeline
  (D5).
* **7.3** Cycle agent steps by connection and role, verdict outputs (D2).
* **7.4** Approval before publish for agent-written cycles (D6).
* **7.5** The Auto Fix example cycle and the user's live gate (D7).
* Later: Reuse context (D5), fork/compare.

## For the user to decide

1. **D1:** workflows are cycles, with no second DSL.
2. **D4:** where the board lives (Agents -> Tasks) and what is on it, and
   whether a table with a "Needs me" filter is enough, or columns by state
   (kanban) are wanted.
3. **D6:** approval before publish for agent-written cycles. Should it also
   cover scenarios agents write?
4. **Order:** start with 7.1-7.2 (board and timeline) or with 7.3 (Codex and
   roles in cycles).
