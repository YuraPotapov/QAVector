# Cycle execution and recovery

A run is a durable execution of a graph. A node's successful result belongs to
that execution, even if a later node crashes. Paying for an agent's answer and
writing that answer to a report are separate operations.

## User actions

| Action | Meaning |
| --- | --- |
| **Run** | Main button before a run or after success. Starts a new execution. |
| **Resume** | Main button after failure, Stop or interruption. Continue the displayed run by its exact ID. Keep successful results; retry unfinished work and affected descendants. |
| **Hard Run** | Dropdown action. Start from the beginning with a new identity, workspace and inputs. Paid services are called again; previous runs stay saved. |
| **Run from here / Run step** | Explicit partial execution using the displayed run's results, or the latest run if none is displayed. Intended for deliberately rerunning a chosen part, including older runs. |

The button's label and the explanation below it show which action a click will
perform. Hard Run confirms the fresh execution and its repeated service calls.

`--cycle-run=development_in_progress --cycle-resume=<run-id>` is the CLI
equivalent of Resume. It cannot be combined with selection flags or variable
overrides. There is no implicit fallback from a refused resume to a new run.
Partial execution accepts `--cycle-reuse=<run-id>` alongside `--cycle-only` or
`--cycle-from`; the GUI pins this to the displayed run so a newer task cannot
silently replace the source of an already reviewed plan.

Resume uses the same workspace: artifact paths and operation receipts remain
valid. Each continuation increments `resume_count` and archives the previous
record in `.execution/history/`. The index includes running executions so a
crashed process does not disappear from history.

## Execution invariants

1. An exclusive OS lock permits one executor per run, across processes.
2. The executor writes an execution manifest and initial state before work.
3. Before starting a node, it records its running state and a digest of its
   resolved settings. A failure to write stops execution before the effect.
4. Before releasing dependent nodes, it writes the completed node's receipt,
   then the aggregate record. Separate node receipts survive a failed summary
   write. JSON replacement and file/directory synchronization make publication
   atomic on supported local filesystems.
5. Observers, UI events and human reports are diagnostics. They do not own the
   authoritative execution state and cannot substitute for a receipt.
6. Failure remains failure. A stored failed gate is never promoted to success
   merely because a user selected a downstream node.

The manifest fingerprints each node's definition, referenced cycle variables
and plugin version. It does not persist resolved credentials. Resume validates
the definitions of retained nodes and the hashes of their artifacts before
executing work. Settings of a failed node, such as its iteration limit, may be
fixed without invalidating an unchanged completed plan.

Unknown steps, missing artifacts, changed completed definitions and uncertain
in-flight effects stop recovery with a reason. They never trigger an automatic
paid recomputation. Original results remain available for inspection.

## Dependencies and refreshed resources

Successful nodes on independent branches remain completed. Failed/incomplete
nodes and their affected descendants are eligible to run; skipped conditions
are evaluated again. A gate's explicit `passed: false` is a business decision,
not a transient service error to retry automatically.

Approval and managed resources (claims, service handles) are refreshed. Refresh
does not erase successful paid work below an old approval. Unfinished consumers
still wait for the fresh approval/resources, including through cached
intermediates. A refusal blocks those consumers. Conditional finalizers may
still write a report.

Resolved input digests protect retained results after refresh. If a new service
handle or other refreshed output changes an input a completed node consumed,
its consumers stop. The executor does not silently pay to rebuild that result.
Repository verification and branch/tree checks remain the plugins' job: a saved
analysis is a snapshot, not proof that the external world has stayed unchanged.

## Paid operations inside a node

`cycle.operations.Operation` is the plugin API for a billable operation. The
key includes the node, operation name and request digest; receipts are scoped
to this run, not a global cross-task cache.

The adapter must:

1. Look for a completed receipt before contacting the provider.
2. Persist `started` before sending the request.
3. Persist the provider's response before parsing, rendering or writing reports.
4. Reuse a successful response to finish local processing after failure.
5. Leave a call without an authoritative response unresolved. A process exit
   code alone does not prove whether the provider charged for a request.

Claude review/edit/implementation and CrewAI/AutoGen review workers use these
receipts. CLI error results retain their session, usage and cost information.
Known failed calls can be retried in a later explicit continuation; successful
calls and their cost records are retained. A plugin may declare `recoverable`
only if it uses durable call receipts: an interrupted node with complete
receipts can finish its local work. Other interrupted effects require checking
the service and saved logs before an explicit retry.

This is not an exactly-once promise for external services. Between a remote
effect and its local receipt there is an unavoidable uncertainty window unless
the provider supports an idempotency key or a request-status lookup. The safe
default is to stop there. New adapters should add provider-specific
reconciliation rather than treating a connection loss as permission to pay
again. Claude conversation resumption is a separate capability; retaining a
finished response does not restore an unfinished model conversation.

### Work a failed node already did

A node run again - by Resume or Run from here - can read what it did last time
through `context.earlier_of(step_id)`, which returns `(run id, StepRun)` from
the run being resumed or reused. `agent.implement` uses it for the expensive
case: an attempt that edited a lot, then failed on a setting somebody has since
fixed. When the checkout's tree hash is exactly the one an earlier run of the
step recorded, that work is what is on disk, so the step checks it (and reviews
it) first. If it holds, the step is verified with no new attempt; if not, the
first new attempt is told what failed and carries on from the edits instead of
starting over. When the run just before never reached the step - stopped at a
gate, for instance - the other runs of the cycle are searched, newest first,
for the same tree. A checkout edited since, or a different branch, has a
different hash and is not continued.

Passing checks are not the last word when the steps after `agent.implement`
in that same run already judged the tree: any high or medium issue a later step
reported (the review, the acceptance against the task) sends the work back.
The first new attempt gets those findings, labelled with the step that raised
them, and reworks the existing edits. Low-severity notes do not send it back.

## Human feedback and plan revisions

An approval can name `revision_step: settle` and `max_revisions: 3`. The GUI
then offers a feedback field and **Send for revision**, separately from approval
or refusal. Typed feedback is not permission to change code. The revision limit
bounds additional paid calls; reaching it leaves approval/refusal available.

The target must be an upstream `agent.review` step. Only read-only review agents
and checks between it and this gate may be revisited. The executor preserves
other completed work, supplies the previous plan and feedback history in
`inputs.user_revision`, runs the planning/review path again, and asks again with
the updated results. Work that depends on approval stays blocked until an actual
approval. A plan cannot be revised after dependent work has already started.

Before revisiting nodes, `.execution/revisions/<number>/` archives the previous
record, node files and feedback. Revision inputs and history are part of the
run record, including during Resume and explicit partial execution. A journaled
multi-node reset survives interruption between individual checkpoint writes;
no new agent call starts until publication has finished. Completed provider
responses keep their existing operation receipts, while changed revision inputs
form a new request. Previously recorded plans never need to be bought again
just to recover the revision state.

## Existing runs

Runs created before execution manifests cannot prove which original node
settings produced their results. Resume refuses those runs. Explicit partial
execution remains available: choose the failed node and **Run from here**.
It accepts skipped optional branches, preserves failure statuses and copies
saved artifacts into the new partial run's workspace. It does not gain the
manifest validation guarantee retroactively. Imported steps retain their
original definition fingerprints and source run. A partial run whose imported
steps have no fingerprint can still be resumed: those steps are held to that
run's own manifest, so Resume refuses if their settings changed after the
partial run started, and otherwise trusts them exactly as far as the partial
run did.

## Regression coverage

Tests exercise repeated writer failures without another paid call, independent
branches, real process termination after result publication, write failures
before and after provider work, uncertain remote outcomes, changed definitions
and refreshed inputs, artifact integrity, exclusive execution, refreshed
approval, exact run selection from the GUI and the launcher, and reconstruction
of an agent report from a saved response. Providers in these tests are local
fakes; they make no paid requests.

## Seeing it happen

Every run says how it began (`cycle.run.mode`): fresh, a resume, or a partial
run, which steps it kept and which it runs again and why. The Subjects list on
the Cycles page shows that beside the run, together with the subject the run is
working on and every earlier run on the same subject.

Ways to go on with a subject, once its row has put its run on the canvas:

- **Resume** continues the same run: same id, same workspace, successful
  results kept.
- **Run step / Run from here** redo part of it, borrowing the rest from that
  run.
- **Delete** removes the session's runs and its memory record, so a later run
  may pick the subject up from the start.

