# Loops in cycles: repeating a part of the graph

Status: **plan, nothing implemented.** Today a cycle is a DAG. `model.problems`
rejects a dependency loop (`_ring_problems`), and every step runs at most once
per run. This document describes what loops would allow, what they would cost,
and the shape we suggest building.

## The problem

Some work is naturally "do it again until it holds". The engine can express
that only in two ways today:

- **Inside one step.** `agent.implement` edits, runs its checks and goes round
  again (`max_attempts`), and `retry:` repeats a single step. Both stay inside
  one plugin. A browser scenario, a second reviewer or a person cannot be part
  of that inner loop.
- **Unrolled by hand.** The In Progress cycle reads plan → review → settle →
  recheck → approve: exactly two review passes, written out as steps. `settle`
  runs on every path, even when nothing was objected to. The graph cannot branch
  and join again, so the later steps need one fixed place to read the plan from.

Everything else ends the run. A rejected approval, a failed scenario after a
service hung, or one bad task in a release batch all mean starting a new run.

## What loops would allow

1. **Work that is finished, not attempted, across plugins.**
   `agent.implement` → `scenario.run` → `agent.review`, repeated while the
   review has blocking findings. The same applies to the plan review:
   `settle` + `recheck` become one loop body that runs only as often as needed.
2. **A person in the loop.** If a person rejects the result with a comment, the
   agent reworks it and the person is asked again. All of this happens in the
   same run, with the same workspace and subject.
3. **One item at a time (foreach).** `pick_tasks` returns several tasks. Today
   they move on as one string (`outputs.joined`). With foreach, each task gets
   its own chain (branch → merge → tests → Jira transition) and its own status.
   One failed task does not stop the others. Ready To Release gains the most.
4. **Retrying a chain, not a step.** If a `service.start` → `scenario.run` chain
   fails, the loop can restart the service and run the scenario again. `retry:`
   cannot do this because it repeats only the step that failed.
5. **Polling and monitoring.** A health check can be a graph: request → check →
   `teams.message` only on failure, with every iteration in the history. Waiting
   fits the same pattern: until a Jira status changes, until a stage comes up
   after a deploy, until a pipeline turns green. A scenario run N times can catch
   flaky tests and races (e.g. two runs contending for one queue row).

## What it costs

Most of the engine assumes that a step id names one execution:

| Area | Assumption today | What changes |
|---|---|---|
| `cycle/model.py` | `_rings` rejects loops; `layers` / `order` compute depth on a DAG | A loop block is one node to the outer graph; its body is validated as its own DAG |
| `cycle/variables.py` | `scope()` maps `steps.<id>` to one `StepRun` | Inside a body, `steps.<id>` is the current iteration; outside, the block exposes the last iteration and the list of iterations; a new `loop` root |
| `cycle/checkpoints.py` | `steps/<step_id>.json`, signatures per step id | Records keyed by an iteration path (`<block>/<n>/<step_id>`); resume rules for an interrupted iteration |
| `cycle/executor.py` | Schedules each step once | Runs a body, evaluates `until`, schedules the next iteration, runs foreach items in parallel |
| Events, history, Subjects | A node has one status | A node has a status per iteration; event volume needs care |
| Canvas, reports | One box, one result | A frame with an iteration counter and a way to look at iteration *n* |
| `cycle/graphrev.py`, `cyclepatch.py` | Revisions edit a flat step list | Decide whether a revision may add steps inside a loop body |
| Budget | Bounded by the number of steps | A loop with agents can spend without limit, so a ceiling is mandatory |

## Proposed design

**No arbitrary back-edges.** A `needs:` that points backwards stays an error.
Loops are **structured blocks**: a step that holds a body of steps. The body is
an ordinary DAG; the outer graph sees the block as one node. Every existing rule
(layers, `needs:`, restrictions, phases) keeps working one level down, and every
record gets one more path segment instead of a new model.

This keeps the principle in `docs/cycles.md`: a loop that is a plugin's own
business (how many times `agent.implement` went round) stays inside the plugin.
Blocks are for loops whose steps are different plugins or people.

### repeat … until

```yaml
- id: plan_review
  label: Review the plan until it holds
  needs: [plan]
  loop:
    max: 3                       # required; the validator rejects a loop without it
    until: "${steps.recheck.outputs.blocking} == '0'"
    delay: 0                     # seconds between iterations
  steps:
    - id: settle
      plugin: agent.edit
      with:
        task: |
          Plan: ${loop.previous.steps.settle.outputs.text}
          Objections: ${loop.previous.steps.recheck.outputs.findings}
    - id: recheck
      plugin: agent.review
      needs: [settle]
```

- `until` uses the existing `if:` grammar (`cycle/conditions.py`) and is
  evaluated after each iteration. That grammar has no numeric comparison, so
  `== '0'` compares text. Adding `<` / `>` is a separate decision (see open
  questions).
- When `max` is reached, the block ends with `stopped_by: max`. Whether that
  counts as failure is up to the block: `on_max: fail` (default) or `continue`.
- If a step in the body fails, the block stops (default). With
  `on_failure: next`, the next iteration starts and can read the failure through
  `loop.previous`.

### foreach

```yaml
- id: per_task
  needs: [pick_tasks]
  foreach: ${steps.pick_tasks.outputs.items}
  as: task                       # ${loop.item} is also available
  parallel: 1                    # how many items run at once
  on_item_failure: continue      # stop (default) | continue
  steps:
    - id: branch
      plugin: git.prepare_branch
      with: {branch: "${loop.item.key}"}
    - id: move
      plugin: jira.transition
      needs: [branch]
      with: {issues: "${loop.item.key}"}
```

- The item list is resolved once when the block starts and stored with the run.
  A resume works on the same items even if Jira has changed since.
- Each item has its own status. The block succeeds only if every item did,
  unless `on_item_failure: continue`. In that case the outputs list which items
  failed.

### References

Inside a body:

- `${steps.<id>...}` means the current iteration's step. Steps outside the block
  resolve as before.
- `${loop.index}` is the iteration number, starting at 1. `${loop.item}` is the
  foreach item. `${loop.previous.steps.<id>...}` is the previous iteration.

Outside the block, `${steps.<block>.outputs...}` gives:

- `iterations`: how many iterations ran;
- `stopped_by`: `until`, `max` or `failure`;
- `last.<step>.outputs...`: the last iteration's outputs;
- `items`: per-item status and outputs (foreach only).

### Records and resume

- Step records move to `steps/<block>/<n>/<step_id>.json`. Steps outside blocks
  keep today's paths, so existing runs still read.
- Finished iterations are kept. An interrupted iteration resumes like a run does
  today: finished steps are kept and the failed one runs again.
- `until` is evaluated again from the records, never from memory.
- Interactions (`user.choice`, `approval.gate`) are asked again in every
  iteration. An answer belongs to its iteration.

### Limits

- `max` is required on every `repeat`. `foreach` is bounded by its item list.
  A configurable engine-wide ceiling (e.g. 50 iterations) applies to both.
- Version 1 supports one level only: no block inside a block. Nesting comes
  later, once the iteration path has proven itself.

## Stages

1. **Model.** Parse `loop:` / `foreach:` blocks. Run `problems` on the body,
   require `max`, and make `layers` treat a block as one node. Make
   `to_document` round-trip blocks. The canvas draws a block as a static frame.
   Nothing runs yet; a cycle with a block is refused at run time with a clear
   message.
2. **repeat.** Sequential iterations in `executor.py`, the `loop` scope root,
   iteration-path records in `checkpoints.py`, resume, and events tagged with
   the iteration.
3. **foreach.** A frozen item list, `parallel`, and per-item status and outputs.
4. **Seeing it.** An iteration counter and picker on the canvas. Iterations in
   reports, run history and the Subjects page.
5. **Using it.** Rewrite the In Progress plan review as a `repeat` and Ready To
   Release as a `foreach` over tasks. Compare them with the current files on
   real tasks.

## Separate, smaller track: periodic checks

Periodic checks do not need graph loops. A `http.check` plugin (URL, expected
codes, maximum response time, number of requests) covers a site health
check. If the plugin is marked `watchable`, a trigger `every:` can run it on a
schedule. Today only `jira.issues` can be watched. This can ship before stage 1.

## Open questions

- Should the `if:` grammar get numeric comparison (`<`, `>`) for `until`, or is
  comparing text enough for version 1?
- Should `retry:` on a block mean "repeat the whole body until it succeeds"? It
  would be shorthand for `repeat` with `until: status == 'success'`.
- May an adaptive revision (`graphrev.py`) add steps inside a loop body? If so,
  does the change apply from the next iteration or only to the current one?
- In a `foreach` over tasks, should each item be its own subject and session?
  Ready To Release suggests yes; a polling loop suggests no.
