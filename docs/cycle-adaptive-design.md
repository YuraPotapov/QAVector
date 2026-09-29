# Adaptive cycles: a plan that can change during a run

Status: **design, with its rules implemented and tested as a pure module
(`cycle/graphrev.py`) that the executor does not call yet.** Nothing in a run
changes shape today. This document is what the next stage is built against.

## Decisions (2026-09-28)

Three questions this design left to the owner were answered:

1. **A person decides.** A revision point asks a person unless its file says
   `approve: policy`, and policy is accepted only for points that may add
   nothing but read-only plugins. `approve: person` is the default.
2. **Partial runs work on steps a revision added.** *Run this step* (the single
   chevron) and *Run from here* (the double one) apply to any step of the run
   on screen, whether the file has it or a revision added it - see *Partial
   runs* below.
3. **Shipped templates do not listen.** The development cycles' Jira triggers
   are `enabled: false` as shipped; a person turns listening on once their
   Jira is set up.

## The problem

The In Progress development cycle is 28 steps, fixed when the file was
written. The agent inside it often learns something the file could not know:
the task needs three checks rather than one, the business rule turns out to
have an edge case that wants its own probe, the first attempt fails in a way
that needs a corrective step. Today the only answers are to write every
possible branch into the file in advance, or to stop and edit the cycle.

Three different needs hide in "the plan should adapt", and they do not need
the same machinery:

| Need | Example | What it takes |
|---|---|---|
| **1. Choose a route** between phases that are already known | bug fix or feature; already done or not | a condition on known steps |
| **2. A variable number of similar tasks** | "write 2-5 checks and run each" | one step template, expanded when the list is known |
| **3. New requirements in the middle** | a check failed; add a corrective step before the summary | new future steps, proposed during the run |

## What the engine already guarantees, and must keep guaranteeing

These are read from the code, not from the plan documents:

- **One owner of run state.** `_Scheduler.drive` is the only writer of the run
  record (`cycle/executor.py`); plugins return results and never touch it.
- **Receipt before release.** A step's receipt is written before anything that
  needs it may start (`checkpoints.Store.step`).
- **Fingerprints.** The manifest holds a digest of every step's definition;
  Resume refuses when a finished step's definition changed, and refuses
  outright when the set of steps changed ("Resume requires the same graph",
  `checkpoints.Store.resume`).
- **Journalled multi-step changes.** A plan revision already changes several
  step records at once through `transition.json`: written first, materialised,
  then removed - and replayed on load if the process died in between
  (`revisions.apply`, `Store.publish`, `Store.load`). A plugin asks for it by
  returning `PluginResult.revision`; the executor applies it.
- **Paid calls are idempotent per request.** `cycle.operations.Operation`
  receipts mean a re-run step reuses a stored provider response.
- **`graph.json` is the graph of the run**, written at start, drawn by the GUI
  for old runs (`cycle/run.py`, `cyclereplay.py`).
- **Not answered is not approved.** A gate with nobody to ask fails.

Any adaptive design that breaks one of these is not an adaptive design worth
having; each section below says how it keeps them.

## Alternatives

### A. Routes and sub-cycles in the static graph

Need 1 is **already expressible** on any output a step declares: an
`agent.review` with `assess: complexity` returns `complexity`, and the first
step of each route carries `if: "${steps.size.outputs.complexity} == 'low'"`
(see *Choosing a route* in [writing cycles](cycles.md)). The rule that a step
with `if:` is not skipped with a failed dependency is what makes the join after
two routes work. What is missing: an agent cannot yet return a free-form route
name (its outputs are a fixed review shape), routing on a gate that failed
leaves the run `failed`, and a route not taken reads as "skipped" - the same
word a failure's victims get.

A sub-cycle step (`cycle.run`, a child run of another cycle) would add reuse,
not adaptivity: it cannot say "three of these" or "one more of these".

- Cost: none in the executor. Resume, partial runs and replay stay as they are.
- Covers: need 1 only.

### B. Research first, then generate a new cycle and run it

The research cycle ends with an agent writing a cycle document; the core
validates it (`model.problems`, the same as any file), a person reviews it on
the canvas - the editor exists now - and it runs as its own run.

- Cost: none in the executor. Everything that exists works on the new file.
- Covers: needs 2 and 3, once, at a boundary between runs.
- Loses: the single run as the audit trail; state has to cross runs (the
  claim, the branch) through the memory store; the session list groups runs by
  cycle, so the second run is another session unless sessions learn to follow
  a subject across cycles; a requirement discovered in the *second* run needs
  a third.

### C. Validated graph revisions inside the run (recommended, in stages)

The file stays a declarative template. A run keeps its **starting graph** and
an ordered list of **accepted graph revisions**. A revision is proposed as
data by a step (an agent, or the engine expanding a template), checked by the
core against a policy the file declares, shown as a diff, approved where the
policy says a person decides, and only then applied - atomically, journalled
like a plan revision, at a point where nothing it touches has started.

- Covers: needs 2 and 3 inside one run, with the audit trail in that run.
- Cost: the executor, the manifest, Resume and the GUI's run model must learn
  that a run's graph can grow. That is the riskiest code in the application,
  which is why C is staged below and why its rules exist first as a pure,
  tested module.

### The choice

**Minimal sufficient complexity per need**:

- Need 1 → **A**. No new engine feature. Improve how a route not taken is
  shown (see the review, P2).
- Need 2 → **C, restricted to `foreach`**: the shape is fixed in the file, only
  the count comes from the run. Deterministic, no free-form proposal, and it
  builds the persistence and Resume machinery C needs.
- Need 3 → **C with revision points**, after `foreach` has proven the
  machinery, and only for future steps, from an allow-list, with a person
  deciding by default.
- **B** stays available as a working pattern today, and as the escape hatch
  when a change is too big for a revision: generate a new cycle, review it in
  the editor, run it.

## The model for C

### Who owns what

| | Owns |
|---|---|
| **The cycle file** | The template, the revision points and their policy. Never written during a run. |
| **An agent plugin** | Research, and a *proposal* as structured output. It cannot change the graph. |
| **`plan.revise` (a core plugin)** | Reads the proposal from its proposer step, asks for approval when the policy says so, returns `PluginResult.graph_revision`. It cannot change the graph either. |
| **The core (`cycle/graphrev.py` + the executor)** | Validates, applies, persists, replays. The only place a graph changes. |

### A proposal

What an agent step returns (as output `proposal`), typed and versioned:

```json
{
  "schema": "qavector.cycle.proposal/1",
  "base_revision": 1,
  "point": "correct",
  "reason": "check_totals failed on a negative discount; the rule needs a guard",
  "add": [
    {"id": "fix_discount", "plugin": "agent.implement", "label": "Guard negative discounts",
     "needs": ["correct"],
     "with": {"model": "sonnet", "directory": "${vars.project_dir}",
              "task": "Reject a negative discount with a clear error.",
              "checks": "${vars.checks}"}}
  ],
  "remove": [],
  "replace": []
}
```

`add` entries are steps in exactly the shape a cycle file has - the same
`to_document` form the editor and `--cycle-save` use - so every existing check
applies to them unchanged. There is no second step format.

### A graph revision

What is stored once a proposal is accepted, in `.execution/graph/<n>.json`:

```json
{
  "revision": 2, "base": 1, "point": "correct",
  "proposal_digest": "sha256...",
  "decision": {"by": "person", "who": "...", "answer": "approved", "at": 1759068000},
  "add": [{"id": "fix_discount", "plugin": "agent.implement", "...": "..."}],
  "remove": [], "replace": [], "before": ["summary"],
  "definitions": {"fix_discount": "sha256 of the step definition"}
}
```

The added steps are kept whole, as proposed and checked, so replaying a
revision never depends on anything but the run directory.

The **effective graph** of a run is the template plus its revisions, applied in
order. It is a pure function of files in the run directory, so it can always be
rebuilt - by Resume, by the GUI replaying an old run, by a report.

### Validation (all of it before anything changes)

`cycle/graphrev.py::validate` - implemented and tested - refuses a proposal,
naming every reason, when:

- **it is stale**: `base_revision` is not the run's current revision;
- **it is out of place**: `point` is not a revision point, or not the one
  asking;
- **it breaks the point's limits**: more steps than `max_steps`, more revisions
  than `max_revisions`, a run bigger than `max_total` steps;
- **a plugin is not allowed**: not in the point's `allow` list - a proposal
  cannot introduce `git.commit`, `jira.transition` or `command.shell` unless
  the author put them there on purpose;
- **an id is taken**: by any step of the run, including one removed by an
  earlier revision (a tombstone - receipts are keyed by step id);
- **it rewrites history**: `remove`/`replace` of a step that is not pending, or
  that this point did not add;
- **it reaches outside the future**: a new step must (transitively) wait for the
  point, and may wait only on the point, on the point's `attach_after` steps,
  or on other new steps;
- **it reads a secret** it was not allowed (`allow_secrets`), or an output its
  producer does not declare;
- **the result is not a cycle**: everything `model.problems` finds in the
  effective graph - unknown plugins, bad settings (`plugin.problems`), a loop,
  a `needs` to nowhere, an `if:` that reads a step it does not wait for.

The graph is left exactly as it was when any of these holds. The point's step
fails (or, with `on_invalid: continue`, succeeds with `applied: false`) and the
reasons are its message and a `cycle.graph.rejected` event.

### The moment of change

In the order the executor would do it, inside `_Scheduler._record` for the
point's step - the same place a plan revision is applied today:

1. **Proposal** - the proposer step has succeeded; its outputs (with the
   proposal) are in its receipt. A paid agent call has an Operation receipt.
2. **Check** - `graphrev.validate(template, revisions, proposal, point, run)`.
3. **Decision** - `approve: person` asks through `cycle.ask` with a preview
   (the new steps, the diff of the graph). No answer, a closed window, no GUI,
   a deadline: rejected. `approve: policy` is allowed only when every
   `allow`ed plugin is read-only (`agent.review`, `check.gate`, `report.*`).
4. **Receipt of the revision** - one journalled transition (`transition.json`):
   the revision record, the new manifest (the added steps' fingerprints and
   `graph_revision: n`), the new `graph.json`, a pending receipt for each
   added step, the point's own receipt (success, `outputs.revision: n`), and
   the run summary. Materialised, then the journal removed.
5. **Event** - `cycle.graph.revised {revision, graph, added, removed}`.
6. **Scheduling** - the scheduler takes the effective cycle; the new steps are
   pending, so the next `_submit` starts whatever is ready.

**If the process dies between 4 and 6**, `Store.load` replays the journal
exactly as it does for a plan revision today: the revision is applied once,
the steps exist once, and nothing has started. **If it dies between 1 and 4**,
Resume runs the point again; the proposer's paid call is served from its
Operation receipt, and a revision record with the same `proposal_digest` for
the same point is recognised as already applied - so a crash can never add the
same steps twice.

### Resume, partial runs, replay

- **Resume** rebuilds the effective graph from the template and the revision
  records and compares fingerprints against the manifest, which includes the
  added steps. It never asks the proposer again for an applied revision: the
  proposal and the decision are in the run. A template changed since the run
  started is refused as it is today.
- **Partial runs** (*Run this step*, *Run from here*) work on the source
  run's **effective** graph, so a step a revision added can be run alone or
  from, like any other (decision 2). The new run inherits the source run's
  revisions, each marked with the run it came from, and borrows every step it
  does not run, as partial runs do today. The one exception: a revision point
  that runs again decides its changes again - its earlier revisions (and any
  made by points they added) are not inherited, and it proposes against the
  graph without them. `graphrev.partial` is this rule, tested. The launcher's
  `--cycle-only` / `--cycle-from` accept such a step when `--cycle-reuse`
  names a run whose effective graph has it.
- **Replay** of an old run draws `graph.json` - the final effective graph - and
  `cycle.graph.revised` events redraw it as it grew. Runs without revisions
  are unchanged.
- **Headless**: `approve: person` fails without the application, like
  `approval.gate`. A cycle meant to run unattended uses `approve: policy` with
  read-only plugins, or no revision points.

### `foreach` (need 2)

The first stage of C, because it exercises all of the above with no free-form
input: the steps a `foreach` adds are fully determined by the file and one
output.

- The template step carries `foreach: {items, key, min, max}`.
- When `items` resolves, the engine - not an agent - makes a revision: one
  instance per item, id `<template>__<key>`. **The key comes from the item**
  (`${item.key}`), not its position, so an instance keeps its id - and its
  receipts - when the list is re-planned.
- `min`/`max` bound the count; outside them the template step fails.
- The instances are a phase on the canvas (`group: <template id>`), so the
  folding that exists today shows them as one box.
- `${steps.<template>.outputs}` is an aggregate: `count`, `passed_count`,
  `failed_count`, and `items` - each `{key, status, outputs}`. `needs:
  [<template>]` waits for every instance.

## Proposed syntax (NOT supported yet)

In the style of the file as it is - `steps`, `needs`, `with`, `${...}`. Two
additions, both optional: a step key `foreach`, and a core plugin
`plan.revise`. A file using either is rejected by an older build as an unknown
key or plugin, so it fails closed rather than running a different graph.

The worked example - *research the task -> settle the business rule -> write
2-5 checks -> run them -> if needed, add corrective steps -> summary*:

```yaml
id: adaptive_task
name: Research, check, correct
version: 2

steps:
  - id: research
    plugin: agent.review
    group: Understand
    with:
      model: sonnet
      repository: ${vars.project_dir}
      task: Find where the rule lives and how it is used today.

  - id: rule
    plugin: agent.review
    group: Understand
    needs: [research]
    with:
      task: State the business rule precisely, and every case it must handle.

  - id: agree
    plugin: approval.gate
    group: Understand
    needs: [rule]
    with:
      question: Is this the rule?
      detail: ${steps.rule.outputs.summary}

  - id: plan_checks
    plugin: agent.review
    group: Check
    needs: [agree]
    with:
      task: >
        Propose 2 to 5 checks that would fail if the rule were broken.
        Return them as checks: [{key, title, command}].

  - id: check
    plugin: command.shell
    group: Check
    needs: [plan_checks]
    foreach:
      items: ${steps.plan_checks.outputs.checks}
      key: ${item.key}
      min: 2
      max: 5
    with:
      command: ${item.command}
    on_failure: continue

  - id: propose_fix
    plugin: agent.review
    group: Correct
    needs: [check]
    if: ${steps.check.outputs.failed_count}
    with:
      task: >
        Some checks failed. Propose the smallest corrective steps, as a
        qavector.cycle.proposal/1 for the revision point "correct".
      inputs:
        results: ${steps.check.outputs.items}

  - id: correct
    plugin: plan.revise
    group: Correct
    needs: [propose_fix]
    if: ${steps.propose_fix.outputs.proposal}
    with:
      proposer: propose_fix
      allow: [agent.implement]
      attach_after: [check]
      before: [summary]
      max_steps: 3
      max_revisions: 2
      approve: person               # the default; written out for the reader

  - id: summary
    plugin: report.html
    needs: [check, correct]
    if: ${run.id}
```

`before: [summary]` is the one place a revision may touch an existing step:
the new steps are added to `summary`'s `needs` - allowed only while `summary`
is pending, so the report waits for the correction instead of racing it.

## Compatibility and migration

- Every cycle written today means the same thing: no key changes meaning, and
  `foreach`/`plan.revise` are absent from every existing file.
- `version: 2` on a file that uses either, so a reader can tell before
  parsing further; older builds refuse the unknown key.
- Old run directories have no `.execution/graph/`: their effective graph is
  the template, exactly as now. Old manifests carry no `graph_revision`: zero.
- The GUI's run model accepts `cycle.graph.revised`; an older GUI ignores the
  event kind (RunState ignores kinds it does not know) and keeps drawing the
  starting graph - wrong in shape, never wrong in status, because a step id it
  does not know is ignored.
- Behind a setting (*Adaptive cycles, experimental*) and a launcher flag until
  stage 3 has shipped: without it, a file using the new syntax is refused with
  a message naming the setting.

## Risks

| Risk | Where it bites | What holds it |
|---|---|---|
| Agent-written settings run arbitrary commands | `command.shell` in `add` | `allow` list; `command.shell` only via `foreach` with commands the file templates |
| Cost blow-up | agents adding agents | `max_steps`, `max_revisions`, `max_total`; a person approves by default |
| Secrets leak into a prompt or a report | `${vars.token}` in proposed settings | `allow_secrets`, empty by default |
| Duplicate steps after a crash | between receipt and scheduling | journal replay; `proposal_digest` idempotency |
| Resume refuses every adaptive run | `set(current) != set(previous.steps)` | effective graph, not the file, is compared |
| GUI shows the wrong graph | RunState seeded once from `cycle.run.start` | `cycle.graph.revised`; Design/Run already separates the file from the run |
| Partial runs become ambiguous | added steps not in the template | the partial run takes the source run's effective graph and revisions (`graphrev.partial`); a point run again re-decides |

## Stages and acceptance

**Stage 0 - now, done.** The three decisions above are recorded. Routes by `if:` on declared outputs documented;
phases and Design/Run in the GUI; `cycle/graphrev.py` validates proposals and
replays revisions, with tests, not called by the executor. Still open for need
1: a `route` output an agent can fill, and "not taken" shown apart from
"skipped".

**Stage 1 - `foreach`.** Executor applies engine-made revisions behind the
flag; manifest and Resume use the effective graph; GUI draws instances as a
phase. Accepted when tests show: stable instance ids across re-planning;
`min`/`max` enforced; a crash after the journal and before scheduling resumes
without duplicates; Resume re-runs a failed instance without re-running its
siblings; an old run still resumes; replay draws the grown graph; *Run this
step* on one instance runs that instance alone and *Run from here* runs it and
what waits for it, borrowing the rest from the run on screen.

**Stage 2 - `plan.revise`.** Agent proposals, the approval preview (added steps
highlighted on the canvas, the settings listed), `cycle.graph.revised` /
`cycle.graph.rejected`. Accepted when tests show: an invalid proposal leaves
the graph byte-for-byte unchanged; a stale `base_revision` is refused; the same
proposal applied twice adds nothing the second time; a proposal naming a
disallowed plugin, a secret, a started step or a taken id is refused with that
reason; no answer rejects; Resume after a crash at every step of *the moment
of change* makes no second paid call and no duplicate step.

**Stage 3 - defaults.** The flag goes; the development cycles gain a
correction point, if the runs show it is worth it.
