# Writing cycles

A **cycle** is a graph of steps that arranges the things this application can
already do — start a service, run scenarios, run a command, write a report — and
says what depends on what. Steps that do not depend on each other run at the
same time.

A cycle is not a scenario. A [scenario](flows.md) is a list of actions driven
against one browser page; a cycle sits a level above and may run a whole
scenario as one of its steps. The two never share a word: the package is
`cycle/`, the files live in `cycles/`, the flags are `--cycle-*` and the events
are `cycle.*`, because the core has used "flow" for a scenario since the
beginning.

Cycles live in `cycles/` beside the rest of your data, and in the tree that
ships with the application. Yours is searched first, so a cycle you write
shadows a bundled one with the same id.

---

## A minimal cycle

```yaml
id: nightly
name: Nightly validation

steps:
  - id: build
    plugin: command.shell
    with:
      command: make all

  - id: tests
    plugin: command.shell
    needs: [build]
    with:
      command: pytest -q

  - id: report
    plugin: report.html
    needs: [tests]
```

The `id` must match the filename: `cycles/nightly.yaml`. Everything else is
optional except `steps`.

Run it:

```bash
python3 session_launcher.py --cycle-run=nightly --events=-
```

Or open it on the **Cycles** page, which draws the whole graph, and press Run.
The nodes take on their status as it goes; there is no second page to watch.

Right-click a project, a cycle, or empty space in the explorer for its actions;
F2 renames, Delete deletes and the menu key opens the row's menu. **Rename
cycle...** changes the display name shown in the tree and page heading; the
cycle id and filename stay the same. **Change id (...)...** is a separate action
that changes the filename and the id used by `--cycle-run`. **Move to project**
rewrites the cycle's `project:`.

A cycle belongs to the project its own file names. The projects list is the
page's list of names - what New Cycle offers, where an empty project lives - so
a cycle naming a project the list does not have (imported, copied from another
machine, or shipped with the application) is shown under that name, marked
*not in your projects list*, with **Add to the projects list** on its menu.
Deleting a project moves its cycles to Unassigned by rewriting their files.

---

## Editing on the canvas

A cycle is YAML, and the YAML tab is the whole of it. The canvas edits the same
file: **Step** in the bar over the canvas adds a step - after the selected one,
when one is selected - and right-clicking a step offers what can be done to it:
open it, add a step after it, choose what it waits for, put it in a phase,
duplicate or delete it. A dependency that would make a loop is not offered.

**Links.** A line from one step to another means the second waits for the
first - its `needs:`. In Design, on a cycle you can edit, a link is made,
moved or removed on the canvas:

| To | Do |
|---|---|
| make B wait for A | drag the **filled dot** under A onto B - or the **hollow dot** on top of B onto A |
| see what a link is | point at the line: *B waits for A* |
| move a link | click the line, then drag its **head** to the step that should wait instead, or its **start** to the step to wait for instead |
| remove a link | click the line and press **Delete** - or right-click it, **Remove this link** |
| set several at once | right-click a step: **Waits for** (what it starts after) and **Runs before** (what starts after it), ticked on and off |

While a link is dragged, the step under the pointer turns blue if the link can
land there and red if it cannot, with the reason next to the pointer: it would
make a loop, or that link is already there. A link that is moved is judged as
if it were already gone, and a move is one change - one Undo. Links added,
moved and removed are part of the draft like every other change. A link to or
from a folded phase stands for several and cannot be picked up; open the
phase. A step's window keeps the same list as text, under **Depends on**.

**Changes collect until Save.** Almost every state between two good files is
one the core would refuse - a step whose required field is still empty, a
dependency on a step not added yet - so the canvas, the Problems tab and the
YAML tab show the draft as the core sees it (a dry run of the save), and the
file is written only when you press **Save**. **Undo** and **Redo** walk through
the changes, **Changes** shows the diff, **Revert** throws them away. Running,
opening another cycle or opening Properties with unsaved changes asks first,
because a run reads the file, not the screen.

**A save keeps what you wrote.** A save from the canvas or a step's window is
applied to the file as edits: the entries that changed are rewritten and
nothing else is, so comments, the order you put things in and blocks of text
survive. A change that cannot keep a comment - a step rewritten whole had one
inside it - shows the diff and asks before dropping it. A file changed on disk
after it was opened is not written over: the save is refused and Reload shows
the other change.

**Design and Run.** The switch over the canvas says which graph it is showing:
**Design** is the file (or your unsaved changes), **Run** is the graph the run
on screen was started with, with its statuses. They are the same picture until
the file changes under a run - and then a run's colours are only shown in Run,
because painting them on a graph that did not run would be a lie about both. A
run starting switches to Run; an edit switches back.

### Phases

A long cycle is mostly a sequence, and twenty-eight boxes down the page is more
than anybody takes in at once. A step may name its phase:

```yaml
  - id: plan
    plugin: agent.review
    group: Plan
```

The canvas frames each phase's steps together under its name, and **Fold
phases** shows each phase as one box saying how its steps went - failed if one
failed, running while one runs. Click a phase's heading to fold just that one;
double-click a folded phase to open it. What is folded is remembered per cycle.
A phase whose steps are joined through a step outside it cannot be one box
without waiting for itself, and stays open.

`group` is how a step is shown, like `label`: it is not part of the step's
fingerprint, so grouping a cycle does not stop its runs from resuming.

---

## Steps

A step names a **plugin** and hands it a mapping. The engine knows nothing about
what any plugin does.

```yaml
- id: backend_tests          # required; letters, digits, _ and - only
  plugin: command.shell      # required; see the plugin list below
  label: Backend tests       # what to call it on the canvas; defaults to the id
  group: Test                # the phase it is drawn in; changes nothing else
  needs: [odoo, postgres]    # which steps must succeed first
  with:                      # this plugin's own settings
    command: pytest -q
  if: "${steps.build.status} == 'success'"
  timeout: 600               # seconds; absent means no deadline
  retry:
    attempts: 3
    delay: 5                 # seconds before the second try
    backoff: exponential     # fixed (default) | exponential: 5, 10, 20...
  on_failure: continue       # stop (default) | continue
  disabled: false
```

`needs:` may be one name or a list; both read naturally.

An unknown key is reported rather than ignored — a silently dropped `timout:` is
a step that quietly never times out.

---

## Shape

`needs:` is what makes a cycle a graph. Anything that does not wait for anything
else starts at once:

```yaml
steps:
  - id: postgres
    plugin: service.start
    with: {service: "Shop/Postgres"}

  - id: odoo
    plugin: service.start
    needs: [postgres]
    with: {service: "Shop/Odoo"}

  - id: backend
    plugin: command.shell
    needs: [odoo]
    with: {command: pytest -q}

  - id: browser
    plugin: scenario.run
    needs: [odoo]
    with: {scenarios: "tag:smoke"}

  - id: report
    plugin: report.html
    needs: [backend, browser]
```

`backend` and `browser` both wait for `odoo` and neither waits for the other, so
they run together. `report` waits for both.

How many steps may run at once is `--cycle-jobs=N`, four by default.

A step is drawn one column past the deepest thing it waits for, so nothing ever
appears to the left of something it depends on.

---

## Plugins

| id | what it does |
|---|---|
| `command.shell` | Runs a command. Streams its output, keeps both streams as files. |
| `time.wait` | Holds this branch until a time. Gives its worker back while it waits. |
| `git.checkout` | Clones a repository into the run, selects a branch/tag or commit, and returns its path and revision. |
| `git.prepare_branch` | Fetches remote branches, finds or creates the exact task branch, and merges the latest remote base into it. |
| `service.start` | Asks the application to start one of its services, and waits for it. |
| `service.stop` | Stops one. |
| `service.restart` | Restarts one and waits for it. |
| `service.wait` | Waits for a service to be up, to print a line, or to reach a named state. |
| `scenario.run` | Runs browser scenarios — the same thing `--run-tests` does. |
| `agent.review` | Reviews a repository and step results; returns structured findings. The default backend needs no API key - it uses the Claude Code CLI's browser sign-in. |
| `agent.edit` | Lets an agent change files in a directory. Edits are applied as they are made. No shell. |
| `agent.implement` | Makes a change and proves it: edit, run the checks, fix what failed, repeat. |
| `git.commit` | Commits the tree that was verified, and refuses anything else. Local only — no push. |
| `jira.issues` | Reads a person's Jira issues and the comments on them. Reads only. |
| `jira.transition` | Moves one issue along its workflow. Writes — and only this. |
| `memory.recall` | What earlier runs remembered about a key. Reads only. |
| `memory.remember` | Writes something into a key's record, or moves a counter. |
| `memory.claim` | Takes a task for this run; given back when the run ends. |
| `approval.gate` | Stops and asks a person. Anything but an answer fails the step. |
| `check.gate` | Several things that must all hold. Names the one that did not. |
| `report.json` | Writes the whole run record as JSON. |
| `report.html` | Writes a readable page describing the run. |

What each takes and produces is published by `--describe` under `cycle_plugins`,
which is where a step's window gets its fields — so this table can go out of
date but the application cannot.

Git checkout and agent setup, including a complete cycle example, are described
in [Git and agent plugins](cycle-agents.md). The default agent backend needs
nothing installed beyond the Claude Code CLI and no API key at all; the CrewAI
and AutoGen backends are optional dependencies installed into the separate
interpreter configured on the step.

```bash
python3 session_launcher.py --describe | python3 -m json.tool | less
```

### Making a change and proving it

`agent.edit` makes one pass and stops, which is right when a person is going to
read the diff. When the step is supposed to *finish* something, `agent.implement`
closes the loop: it edits, runs the checks you name, and when they fail it goes
round again with the failure in front of it.

```yaml
  - id: impl
    plugin: agent.implement
    with:
      model: sonnet
      effort: high              # low, medium, high, xhigh, max; blank uses the CLI's own
      directory: "${vars.project_dir}"
      checks: |
        python3 -m pytest -q
        python3 -m ruff check .
      max_attempts: 3
      task: Guard average() against an empty list.
```

**The loop is inside the step, not in the graph.** What a person reads off the
canvas is "make the change and prove it" — one thing, that either held or did
not. How many times the agent went round is the step's own business, the way
`retry` is. A cycle file cannot express a loop anyway (the engine is a DAG and
says so), but that is not why it is here: a counter on the canvas would not be
structure.

**`verified` is not the agent's opinion.** It is true only when every named
check exited zero *and* — unless you turn it off — a second agent, given the
reading tools and nothing else, signed the result off against the acceptance
criteria. Tests passing while a reviewer objects is exactly the case the gate
is for. Only `high` and `medium` findings send the work back; a `low` note is
recorded and not acted on, because spending the budget polishing something that
already works is the opposite of what the budget is for.

**At least one check is required.** A step called implement that verifies
nothing would report success on any edit at all. If that is what you want, the
step you want is `agent.edit`.

### Committing what was verified

```yaml
  - id: commit
    plugin: git.commit
    needs: [impl]
    with:
      directory: "${vars.project_dir}"
      expect_tree: "${steps.impl.outputs.tree}"
      message: "${steps.plan.outputs.summary}"
```

Committing is one command; `command.shell` can do it, and the demo cycles do.
What a shell step cannot do by itself is verify the tree, the intended branch,
and whether this operation has already made a commit.

`git.commit` accepts `expect_branch`, normally `${steps.todo.outputs.key}`.
It checks the actual current branch before staging and immediately before
committing. A different branch or detached HEAD fails with the expected and
actual names; it does not switch branches automatically.

### A plan that was objected to gets a second pass

A review of the plan used to end the run: a gate asked for zero blocking
findings, and one medium note left the whole thing skipped from the approval
down. The plan never got a chance to answer the objection, the attempt was
spent, and the next run started again knowing nothing about what the reviewer
had said.

The In Progress cycle now reads **plan → review → settle → review again →
ask a person**:

- `settle` is handed the plan, the objections and the rules, and closes what a
  plan can close. An objection it cannot close without a decision that is not
  its to make stays open and is named in the summary. It begins its summary
  with `Unchanged.` or `Revised.`, so a reader can tell at a glance which
  happened; with nothing blocking it repeats the plan word for word.
- `recheck` is a fresh reviewer reading only the settled plan — the same
  separation `agent.implement` keeps between writing and signing off.
- `approve` then asks a person, **always**, showing the settled plan, both
  reviews' counts and whatever is still open. Nothing machine-made stops the
  run before that question; `preflight` requires only that a person agreed,
  that the task is still claimed and that it is the same task.

The engine is a DAG, so `settle` runs on every path rather than only on the
one where something was objected to. That is what keeps the steps after it
reading one fixed place for the plan instead of guessing which of two steps
produced it.

The **Development cycle (In Progress)** prepares the branch before examining
the repository or making a plan. Its `git.prepare_branch` step fetches `origin`
(`git_remote`) on every run, including pruning deleted remote branches. It uses
the exact issue key as the branch name, for example `QA-7`:

- An existing local branch is checked out and merged with its remote counterpart
  when one exists, then with the freshly fetched `<git_remote>/<git_base>`.
- A remote-only task branch is checked out locally and merged with that base.
- A missing task branch is created from the fetched base commit.

The base is configured by `git_base`; the local branch of that name is not used
while the remote answers. Uncommitted changes and unfinished Git operations stop
preparation. Merge conflicts remain available for resolution and block the
dependent agent steps. Nothing is pushed. `project_dir` must point to the actual
Git checkout, including when it is nested inside a larger project.

**When there is nothing to fetch.** By default a failed fetch stops the step:
the branch is meant to start from the base as it is now. With `offline: true` —
which the shipped In Progress cycle sets — the step says in its output that
nothing was fetched, then prepares the branch from what the checkout already
has: the last fetched base if there is one, otherwise the local base branch. It
names the ref and the commit it used, and reports `offline` and `base_ref` as
outputs, so a later step or a reader can tell how old the base is. Nothing else
about the step changes: a dirty tree, an invalid branch name or a merge
conflict still stop it, and a checkout with no base at all fails rather than
inventing one.

**A checkout with no remote at all** carries on that way whatever the settings
say. There is nothing unreachable about a repository that was never given a
remote, and nothing misconfigured either; the only thing that makes a directory
unworkable here is not being a git checkout. A remote that is *named* and does
not exist is different — `orgin` for `origin` should not quietly build on a
stale base — so that is reported, naming the remotes the checkout does have,
and follows `offline` like a failed fetch.

**Is this the tree that passed?** `agent.implement` records the git tree hash at
the moment the checks agreed, and `git.commit` refuses if the files have changed
since. So "what was verified" and "what was committed" are the same object
rather than two descriptions that are usually the same. The hash is taken
against a temporary index, so a refusal leaves your own staging exactly as it
was.

**Did I already do this?** A run killed between the commit and the record leaves
a commit nobody knows about, and re-running would make a second. So the intent
is written into the message as a `QAVector-Operation:` trailer and the next
attempt looks for it first, reporting `created: false` when it finds its own
work. The intent names the tree and the message, deliberately **not** the
parent — the parent is the one thing committing changes, so an id that included
it would never match on the run that needed it to.

A hook that rewrites the tree as the commit is made fails the step and names the
commit it made, rather than committing again on top.

It is local only: no push, no branch, no merge, no tag. Anything that reaches
other people is a step this does not have.

### Moving an issue along

`jira.issues` reads and only reads. When a cycle should tell the board it has
picked something up, that is a different step:

```yaml
  - id: start
    plugin: jira.transition
    with:
      site: "${vars.jira_site}"
      email: "${vars.jira_email}"
      token: "${vars.jira_token}"
      issue: "${steps.todo.outputs.key}"
      to: Start Progress          # the transition, as YOUR workflow names it
      lands_in: In Progress       # where it should end up
      comment: Picked up by QAVector.
```

**Name the transition, not the status.** A workflow is the project's own, and
the move out of To Do is "Start Progress" in one instance and something else in
the next. The step names what it wants, the plugin asks the issue what it can
actually do, and a name that is not on offer is refused **with the names that
were** — which is the difference between a usable error and a 400.

**`lands_in` is what makes a re-run safe.** A workflow stops offering a move
once it has been made, so a run killed between the transition and the record
would come back to a 400. Told where the move should end up, the step
recognises an issue already there, reports `changed: false` and succeeds. It is
also checked afterwards: landing somewhere else — a post-function moved it on —
fails the step rather than reporting a success nobody verified.

**`expect_status` refuses when the issue moved under you**, the same idea as
`git.commit`'s verified tree.

**It cannot say who moved it.** Finding an issue in the target status proves
the status, not the author. Where that matters, record your own intent with
`memory.remember` before the step and read it back after.

Use `${steps.todo.outputs.key}` — the first issue's key — rather than `keys`:
`${...}` cannot index a list, so a step handed `keys` gets the whole list where
it wanted one issue.

### What a cycle remembers between runs

A run otherwise starts knowing nothing. It cannot ask whether this task was
already done, how much of a budget it has spent, or whether another run is on
it right now — the `${...}` scope has four roots and every one is about the run
in progress. The three `memory.*` steps are the store that answers those.

```yaml
  - id: seen
    plugin: memory.recall
    with:
      key: "${vars.issue}"
      field: status

  - id: work
    plugin: agent.implement
    needs: [seen]
    if: "${steps.seen.outputs.value} != 'done'"
    with: {...}

  - id: note
    plugin: memory.remember
    needs: [work]
    with:
      key: "${vars.issue}"
      fields: {status: done}
      bump: [runs]
```

**Guard on `value`, not on `fields`.** `recall` returns the one field you named
as `value`, empty when nothing was remembered — so the condition holds on the
first run too. Reaching into `${steps.seen.outputs.fields.status}` **fails the
step** when that name is not in the record yet, and on a first run no name is:
a guard written that way works from the second run onward and breaks on the one
that matters.

**Keys mean whatever your cycle says.** A key is a string you compose out of
your own variables. The core does not decide what a task is, so two cycles that
disagree about identity simply do not collide.

**Counters go in `bump`, not in `fields`.** A counter read and written by two
runs loses one of them; the store moves it under its own lock instead. That is
what makes a budget that survives a restart actually bound anything.

**A budget is read before it is spent.** Judge the count as it was when the run
started and move it once the run is admitted — a gate that compares a number it
has just incremented charges the task for being asked, so a run turned away for
any other reason (already done, nothing assigned, a dirty checkout) eats an
attempt it never used. Reading needs `default`, because on a task nobody has
touched the counter does not exist yet and `''` compares as *not numbers*:

```yaml
  - id: spent
    plugin: memory.recall
    with: {key: "${vars.issue}", field: attempts, default: "0"}

  - id: admit
    plugin: check.gate
    needs: [spent]
    on_failure: continue
    with:
      checks:
        within its budget: "${steps.spent.outputs.value} < ${vars.max_runs}"

  - id: budget
    plugin: memory.remember
    needs: [admit]                 # only an admitted run costs an attempt
    with: {key: "${vars.issue}", bump: attempts}
```

Still *before* the work rather than after it: a run that dies half way through
has spent an attempt, and a counter written at the end would give a crashing
task infinite tries. Both development cycles are wired exactly this way.

### Not starting the same work twice

```yaml
  - id: claim
    plugin: memory.claim
    with:
      key: "${vars.issue}"
      seconds: 3600
```

A second run of the same task fails at this step, naming who holds it, and says
so plainly: *this is not a failure of the work, only of the timing.* The claim
is **given back when the run ends** — it is a held handle, so a crash, a
timeout and a Ctrl+C all release it without the step knowing about any of them.
It also expires, so a machine that died does not hold a task until somebody
notices.

**The store is plain JSON, and locked.** Unlike the secrets file it is not
encrypted, because "why did it skip that task" is a question somebody will ask
and the answer being readable is worth more than a confidentiality it does not
need — so do not put a credential in it. Unlike the secrets file it also takes
a lock around every write: two runs both believing they hold a task is the one
thing a claim exists to prevent.

Read it from a terminal with `--cycle-memory-list`, `--cycle-memory-show=KEY`
and `--cycle-memory-forget=KEY`; the path is in Settings → Cycle memory.

### What a cycle works on

A development cycle's run is about one task; the next run may be about the same
task - a resume, the plan sent back, a second try a week later - or about the
next one in the queue. Say which, and the runs group themselves:

```yaml
variables:
  task_key: ""                              # empty: the next one in the queue

subject:
  kind: task                                # a word for the reader
  key: ${steps.todo.outputs.key}            # required: the identity
  title: ${steps.todo.outputs.title}        # optional
  memory: dev/${steps.todo.outputs.key}     # the memory record holding its state
  pin: task_key                             # the variable that fixes it

steps:
  - id: todo
    plugin: jira.issues
    with:
      issue: ${vars.task_key}               # set: exactly that task
      ...
```

The run works the subject out as it goes: once every step `key` reads has
succeeded, the run records `{kind, key, title, memory, pin, step}` - `step`
being the one that settled it - announces it as a `cycle.subject` event, and
keeps it in `metadata.json` and the run index. A key that comes out empty is
no subject: a queue with nothing in it has not found one.

**A session** is every run of one cycle on one subject. The Subjects list on
the right of the Cycles page shows one row per session - what it is about,
where it got to, what the cycle remembers about it - and clicking a row puts
its latest run back on the canvas, so Resume continues that one. A cycle with
no `subject:` gets a row per run: every pass stands on its own. So does a run
that stopped before it found its subject - until resuming it finds one, and it
joins that subject's session.

**`pin`** names the variable that fixes the subject, and it has to be one the
cycle's steps actually read - `jira.issues`' `issue` field is made for it.
A fresh run on one task is then `--cycle-var=task_key=QA-934` from a
terminal. In the application a session is continued with Resume, Run step or
Run from here once its row has put its run on the canvas.

**Deleting a session** removes its run directories, their rows in the index,
and the `memory` record - so its attempt count and any "committed" mark go with
it, and the cycle may take the task up again. It never touches a branch, a
commit or an issue, and it is refused while one of its runs is going.

A subject cannot read a secret: it is written to disk and shown on screen.

From a terminal: `--cycle-sessions[=CYCLE]` lists them as JSON, and
`--cycle-session-delete=ID` deletes one.

### Starting when a new task appears

A cycle can listen for its own work. A trigger names the step the cycle takes
its task from, and while the application is open that step is asked every
`every` seconds what it would take now. Only a step whose plugin says it can be
listened to (`watchable`, published by `--describe`) may be watched; today that
is `jira.issues`, and the rest of this section uses it as the example:

```yaml
subject:
  kind: task
  key: ${steps.todo.outputs.key}
  pin: task_key

triggers:
  - id: new_task
    watch: todo        # a step whose plugin is watchable - here jira.issues
    every: 300         # seconds; 60 at the least
    # pin: task_key    # defaults to the subject's pin
```

A task the listener has not seen before is offered in a window - *Start work on
QA-7?* - with four answers. **Start work** runs the cycle with `pin` set to that
task's key; the cycle still asks its own approval before any code is written.
**Ignore** means it is never offered again. **Not now** asks again at the next
check. **In plan (20)** counts down on its own button and presses itself at
zero: a task nobody answered about goes on the plan instead of holding up every
task behind it. Several new tasks are asked about one window at a time.

**The plan** is in the Subjects list on the Cycles page, under *+ New run*, the
open cycle's first: each task with when it was planned, *Start work* for the one
picked, and a bin that takes it off (and does not offer it again). It is kept in
the memory store, so it survives a restart. `--cycle-watch-planned` lists it and
`--cycle-plan-remove=ID` takes an entry off.

The first check a trigger ever makes only remembers what is already in the
queue, so turning a listener on does not greet you with a question per open
task. It asks one task at a time, and never while a run is going: a task that
turns up during a run is offered when it finishes. A check that fails - the
source unreachable, a secret not set - is retried on schedule without a dialog.
A secret the watched step reads and nobody has set is refused before anything
is asked, so a template's placeholder site is never sent a blank credential.

The step's settings are resolved from the cycle's variables and secrets; they
cannot read another step's outputs, since no step has run. What the plugin does
with them is its own: `jira.issues` runs the step's query - project, JQL,
ordering - with `issue` ignored, since a listener pinned to one key would never
see another. What has been seen is kept in the memory store under
`watch/<cycle>/<trigger>`.

The development cycles that ship with the application carry their trigger
switched off (`enabled: false`): a template has nobody's Jira in it yet. Set the
Jira variables and the token, then turn listening on as below.

**Turning it on and off.** Double-click the watched step: a step whose plugin
can be listened to has a *Listen for new work* section with **Listen** and
**Every (seconds)**. Unticking Listen writes `enabled: false` into the trigger
rather than deleting it, so ticking it again keeps the schedule; ticking it on
a step that never had one adds a trigger for it. Like everything else in that
dialog, it is saved in the cycle file.

**Seeing it work.** While anything is listened for, the status bar says so -
*Listening: 1 cycle - next check 0:42* - with each cycle's own schedule in its
tooltip. A check that failed turns it red and says why; that is the only place
a failure shows, since a listener must not interrupt anybody with a dialog
because the network is down.

From a terminal: `--cycle-watch=CYCLE[:TRIGGER]` prints what is new (it starts
nothing), and `--cycle-watch-seen=CYCLE[:TRIGGER]:KEY` marks one task seen.

### How a run began

Right after `cycle.run.start` every run says how it began, as `cycle.run.mode`:

| `mode` | What it means |
|---|---|
| `fresh` | Nothing taken from anywhere. |
| `resume` | The same run again: `kept` lists what it took from its last attempt, `rerun` what it does again, each with its reason - *failed last time*, *waits on a step that runs again*, *asks again on every run*... |
| `partial` | Run step / Run from here: `kept` is what was borrowed from `source_run`, `rerun` what was chosen. |

A plan sent back from an approval is a `cycle.revision` event with its round,
the feedback, and the steps it revisits. The Subjects pane shows all three.

### Waiting for a time

`needs:` says a step goes after another one. `time.wait` says a step goes after
a **time** — and everything that needs it waits too.

```yaml
  - id: settle
    plugin: time.wait
    needs: [deploy]
    timeout: 600
    with:
      seconds: 120
      reason: letting the deploy settle

  - id: smoke
    plugin: scenario.run
    needs: [settle]
```

Either `seconds:` (from when the step first started) or `until:` — a clock time
like `09:00`, or a date and time like `2026-09-22T09:00`. A time on its own
means today, or tomorrow if it has already gone by.

**It is not `command.shell: sleep`.** A sleeping command holds a worker for the
whole sleep, so a cycle with four of them and the default four jobs stops
running anything at all. A waiting step **gives its worker back** and is woken
when its time comes, so a run may be waiting on a dozen things and still be
working on everything else. It is also visible: the node reads `waiting` with
what it is waiting for, rather than looking like a command that has hung.

**A deadline still wins.** The step's own `timeout:` bounds the whole wait, not
each turn of it, so `seconds: 3600` under `timeout: 60` ends after the minute.
Two ways of saying how long something may take, and the smaller one is the one
that means anything.

Any plugin can do this, not just this one — a plugin returns
`registry.waiting(seconds)` to say *I am not finished, and there is no point
asking me again before then*. That is deliberately not a retry: a retry is what
happens after a failure, and a reader who cannot tell the two apart sees a
healthy cycle as one failing over and over.

### Deciding on several things at once

A condition on a step is deliberately one comparison — `if:` has no `and`, no
`or` and no `<`, because half an expression evaluator is worse than a small
complete one. That is right for an edge in the graph and wrong for the place a
run decides whether it has earned the next step, which is usually a conjunction.

```yaml
  - id: final
    plugin: check.gate
    needs: [implement, integration, review]
    on_failure: continue
    with:
      checks:
        the work proved itself: "${steps.implement.outputs.verified} == true"
        the checks passed: "${steps.integration.outputs.exit_code} == 0"
        the review is clean: "${steps.review.outputs.blocking_count} == 0"
```

Operators are `==`, `!=`, `<`, `<=`, `>` and `>=`, each with a space on either
side. All the checks must hold; the step fails otherwise and **the message
names the one that did not** — "the review is clean: 1 == 0" rather than
"it did not pass". They are read in order, so write them cheapest first.

The GUI shows the same decision as a row in **Stages**: the verdict in words,
then every check with a mark saying which way it went and the values that were
actually compared. So a gate that refused is read at a glance rather than by
matching a heading against a list of comparisons.

`${...}` is resolved before the plugin sees it, so what it parses is already
`1 > 0`. A value that might contain an operator of its own goes in quotes.

**How a gate branches.** `on_failure: continue` on the gate, and nothing after
it: a step whose dependency did not succeed is skipped, and the chain dies by
itself. To run something *because* the gate refused, give that step its own
`if:` — a step with a condition is not skipped along with a failed dependency,
which is the rule that makes the other branch reachable at all.

### Asking a person

Everything else a cycle does, it decides for itself. `approval.gate` is the one
step whose answer is somebody's, for the places where that is the honest
arrangement: before an agent edits a repository other people work in, before
work moves on a board other people read.

```yaml
  - id: approve
    plugin: approval.gate
    needs: [review]
    timeout: 600
    with:
      question: Apply this fix?
      detail: "${steps.review.outputs.summary}"
      options: [Apply the fix, Leave it alone]
```

The first option is the one that means yes; name `approve:` to have more than
one mean it. The step's own `timeout:` is the deadline — written where every
other deadline in the file is, rather than in a field of its own — and the
window counts it down so nobody decides after it has stopped mattering.

**Not answered is not approved.** No application attached, nobody at the
screen, the window closed, the deadline passed — every one of those fails the
step, and there is deliberately no setting to change it. A gate that approved
when it could not ask would stop being a gate exactly where somebody believed
they had one.

That is also its cost, and it is a real one: a run that reaches a gate at three
in the morning waits out its deadline and then fails. **A cycle meant to run
unattended should not have one — and should delete the step rather than disable
it**, because a disabled step skips everything that needs it, which would
quietly turn off the work as well as the question.

`cycles/review_and_fix.yaml` has one, between reading the code and changing it:
the findings are in front of you, nothing has been written yet, and the agent
that costs money has not started.


**Nobody answered goes on the plan.** A gate that times out, or finds no
application to ask in, still fails - not answered is not approved, and nothing
after it runs. But the question is kept on the plan with the run it belongs to:
the Subjects list shows *QA-4 waiting for your approval*, and **Resume** there
continues that run and asks again. A refusal is an answer and is not planned;
nor is a run somebody stopped.
### Reading Jira

Each successful `jira.issues` step writes two neighboring artifacts:
`jira.json` keeps the technical data and `jira.html` is a standalone reading
copy with issue details, formatted descriptions, tables, lists and comments.
Cloud formatting comes from the document bodies already returned by Jira;
no extra requests are needed. Plain-text bodies remain readable too.
Select `jira.html` on the Artifacts page to read it directly in the preview.
The **Preview / Source** selector switches between the formatted page and HTML
source; **Open in OS** opens the file in your browser.
`report_path` still points to JSON; `html_path` points to the reading copy.
Existing artifacts are not rewritten.

`jira.issues` answers one question — *what is on this person's plate, and what
has been said about it* — and does nothing else. There is no create, no
transition and no edit, so a step can be pointed at a production instance
without thinking about it.

```yaml
variables:
  jira_token: {secret: true}

steps:
  - id: mine
    plugin: jira.issues
    with:
      site: https://yourcompany.atlassian.net
      flavour: cloud                 # or server, for Jira Server / Data Center
      email: you@yourcompany.example
      token: "${vars.jira_token}"    # never the value itself - see below
      user: currentUser()            # or an account id on Cloud, a username on Server
      project: QA, WEB               # blank reads every project the account sees
      role: any                      # assignee, reporter or creator
      status: In Progress, Reopened  # blank is any status
      jql: updated >= -14d
      limit: 25
```

**Projects.** Blank is every project the account can see, which is the right
default for "what is on this person's plate" — work crosses projects and few
people think in one. `project` narrows it: one key, or several separated by
commas, which becomes `project in (...)`.

**Statuses.** `status` is the board's column, by the name your workflow gives
it: one, or several separated by commas, which becomes `status in (...)`. It is
its own field rather than something written into `jql` because it is usually
the one thing a step per column changes — set one step up, duplicate it, change
the status.

**Everyone.** `role: everyone` leaves the person out — *Ready for prod,
whoever carries it* — and `user` is then not needed. It needs `status`,
`project` or `jql` to say which issues; on its own it would be every issue the
account can see, and the step refuses it.

**Sub-tasks.** `subtasks: full` (the default) reads each issue's sub-tasks the
way the issue itself was read — description, comments, pictures — in one more
search, and puts them in the parent's `subtasks` list, each with its `parent`
key. `list` keeps only the key, title and status the search already returned,
for no extra request; `none` leaves them out. `keys`, `key` and `count` are
still about the parents, so `limit: 1` still takes one task from the queue;
`subtask_count` says how many sub-tasks came with them. A sub-task the account
may not browse keeps what its parent said of it rather than disappearing.

**Attachments.** A bug report's specification is often a screenshot, so the
step downloads the pictures on the issues it read and says where they landed.
Each issue carries an `attachments` list of
`{filename, mime, size, created, author, url, path}`, and the step also
publishes `attachments` (every file, flattened, with its issue `key`), `images`
(just the downloaded pictures' paths) and `attachment_count`. Files land under
the step's own directory and are listed as artifacts of the run.

```yaml
      attachments: images            # or all, or none
      attachment_bytes: 25000000     # the step's whole budget, not per file
```

`images` is what to hand a later step that can look at them. A file that was
not downloaded — the wrong kind, or past the budget — is still listed, with an
empty `path` and the `url` it is at, so an issue never looks as though it had
nothing attached. A download that fails costs that one file and not the issue.

An image inside a description is not dropped either: it becomes
`[image: name]` in the text. It used to vanish without trace, which is worse
than it sounds — a task whose whole specification was a screenshot arrived as
a description that did not mention one, and whatever read it planned the work
from half the page.

`jira.html` shows the pictures themselves, embedded, where the description put
them — a screenshot means very little away from the sentence it belongs to.
Each is captioned with its filename: ADF keeps an embedded file under an id the
attachment API never reports, so a node and a file are matched by the node's
alt text when there is one and by position when there is not, and the caption
is how a reader can tell when that went wrong. Anything the body never showed —
a file nothing refers to, one that is not a picture, one past the budget — is
listed under the description instead, so an issue never reads as having nothing
attached. The pictures are embedded as `data:` URIs rather than linked, so the
page stays a single file somebody can send on, and its content security policy
still loads nothing remote.

Worth knowing either way: `limit` cuts **after** `ORDER BY updated DESC`, so on
a busy account one loud project can fill the whole page and a quiet one never
appears at all. Nothing breaks; it is simply not in the answer. Raise the limit,
name the projects, or narrow the window with `updated >= -14d`.

**The token belongs in the secrets store, not in this file.** Cycle files are
committed and ship inside the build, so a value written here travels to everyone
who clones the project. Declare it `{secret: true}`, set the value in Settings →
Cycle secrets, and reference it. That arrangement exists because a live API key
ended up in a cycle file once already.

**Cloud and Server are different products here.** Atlassian removed the old
search endpoint from Cloud and returns comment bodies as a document tree;
Server keeps the older endpoint and sends comments as text. Nothing about a URL
says which you have, so `flavour` is a field rather than a guess. On Cloud the
credential is your email plus an API token — not your password; on Server it is
a personal access token with `auth: bearer` and no email.

`issues` comes back as a list a later step can walk, each one carrying
`key`, `summary`, `status`, `assignee`, `reporter`, `updated`, `url` and its
`comments`. Display names only: an email address arrives in every issue, nothing
downstream needs it, and a step's outputs travel into reports and into agents'
prompts.

```yaml
  - id: triage
    plugin: agent.review
    needs: [mine]
    with:
      model: sonnet
      inputs:
        issues: "${steps.mine.outputs.issues}"
      task: Which of these are blocked, and on what? Cite the issue keys.
```

`cycles/task_to_commit.yaml` goes the whole way: one task in To Do, a plan, the
change made and proved, and a local commit of the tree that passed. Like the
one below it is a template — it needs your Jira and a checkout to work in.

`cycles/todo_to_plan.yaml` is that idea finished: it reads what is in To Do for
one person, with the comments, and hands the lot to an agent to turn into an
ordered plan. Unlike the other demo cycles it is a template — it needs your
Jira — so fill in its four variables and put the token in Settings → Cycle
secrets.

It is also the shortest example of the two conditions worth knowing. `plan`
carries `if: ${steps.todo.outputs.count}`: an empty list is falsy, so nothing in
To Do skips the agent step rather than paying for one to be told there is
nothing to plan. `report` then carries `if: ${run.id}` — a run always has an id,
and a step with a condition of its own is **not** skipped along with a skipped
dependency, so the report is written either way.

### Services need the application

`service.*` steps ask the GUI to act, because the services on the Services &
Logs page are owned by that process. Run a cycle from a terminal and they fail
immediately, saying so, rather than waiting out a timeout — which is what makes
a cycle with service steps safe to try headless. Under the GUI they work
normally.

A service a step started is **stopped again when the run ends**, in reverse
order, so a cycle is repeatable. `keep: true` leaves it up.

`approval.gate` works the same way and for the same reason: it reaches a person
through the application, so headless it says so at once instead of waiting.

---

## Outputs

A step does not know about the steps around it. It reads what an earlier one
produced:

```yaml
- id: postgres
  plugin: service.start
  with: {service: "Shop/Postgres"}

- id: migrate
  plugin: command.shell
  needs: [postgres]
  with:
    command: "psql -p ${steps.postgres.outputs.port} -f migrate.sql"
```

What can be referenced:

```
vars.<name>                     the cycle's variables, as the run started
env.<NAME>                      the process environment
run.id / run.workspace / run.cycle
steps.<id>.status               success | failed | skipped | ...
steps.<id>.outputs.<key>        whatever that plugin returned
steps.<id>.artifacts.<name>     a path, relative to the run's workspace
steps.<id>.message / .attempts / .duration_ms
```

A reference that is the **whole** value keeps its type, so
`"${steps.postgres.outputs.port}"` stays a number. One embedded in a longer
string becomes text, which is the only thing it could do.

A reference to something that does not exist is an error, not an empty string —
and it names what was there instead, so a typo is usually fixed from the message.

### Variables

```yaml
variables:
  branch: main
  suite: smoke

steps:
  - id: checkout
    plugin: command.shell
    with:
      command: "git checkout ${vars.branch}"
```

Override one for a single run:

```bash
python3 session_launcher.py --cycle-run=nightly --cycle-var=branch=release
```

---

## Conditions

```yaml
- id: debug
  plugin: command.shell
  needs: [tests]
  if: "${steps.tests.status} == 'failed'"
  with:
    command: ./collect-diagnostics.sh
```

Three forms, and no more:

```
${reference} == 'literal'
${reference} != 'literal'
${reference}                  (truthy: "", 0, false, no, off and null are false)
```

Three things about `if:` are worth knowing.

**It overrides the failed-dependency rule.** Normally a step whose dependency did
not succeed is skipped. A step with an `if:` is not: the author has said they
will decide. That is what makes the example above possible — it both waits for
the tests and runs because they failed.

**A skipped step has no verdict.** If a condition reads an output from a
skipped step that produced none, the conditional step is also skipped. For
example, an absent `passed` output does not count as `false`: the gate never
ran. Status comparisons still work, and an unknown step or a missing output
from a step that ran still reports an error.

**It must `needs:` what it reads.** A condition on a step that does not wait for
the step it mentions is evaluated before that step has run, so it silently never
fires. This is reported when the cycle is read, rather than left to be
discovered at three in the morning.

**Choosing a route.** When a step decides which way the work goes, route on
one of its outputs: a condition on the first step of each route, and a step
after both routes that carries its own `if:` so it is not skipped with the
route not taken:

```yaml
- id: size
  plugin: agent.review
  with:
    task: How much work is this change?
    assess: complexity              # outputs.complexity: low, medium, high...

- id: quick_fix
  plugin: agent.edit
  needs: [size]
  if: "${steps.size.outputs.complexity} == 'low'"
  with: {...}

- id: full_plan
  plugin: agent.review
  needs: [size]
  if: "${steps.size.outputs.complexity} != 'low'"
  with: {...}

- id: report
  plugin: report.html
  needs: [quick_fix, full_plan]
  if: ${run.id}
```

Route on an output rather than on a gate failing: a `check.gate` with
`on_failure: continue` does let the other branch run, but the run then ends
`failed`, which reads wrong when the gate only chose a route.

A plan that grows new steps while it runs is not this; see
[the adaptive cycle design](cycle-adaptive-design.md).

---

## Failure

The main **Run** button becomes **Resume** after failure, Stop or interruption.
Resume continues the displayed execution using its saved successful results.
**Start over (new run)** in the dropdown starts the graph from the beginning and
calls paid services again. Resume is also available as
`--cycle-run=ID --cycle-resume=RUN_ID`. It validates retained results and stops
on changed inputs or uncertain external effects instead of silently repeating
paid work. See [execution and recovery](cycle-recovery.md) for the persistence,
plugin and migration contracts.

By default a failed step stops the run: everything not yet started is cancelled.

```yaml
on_failure: continue
```

lets the rest carry on. Steps that depended on the failed one are still skipped —
they were written against outputs that do not exist — but unrelated branches run,
and the run still ends `failed`.

---

## Timeouts, retries and stopping

```yaml
timeout: 300          # seconds for the whole step, retries included
retry:
  attempts: 3         # total tries, not extra tries
  delay: 5            # seconds before the second try
  backoff: exponential  # doubles the delay before each further try
```

`backoff: exponential` waits 5, then 10, then 20 seconds; `fixed`, the default,
waits 5 every time. The step's `timeout:` still bounds the whole of it, so a
long run of doublings ends where the deadline says. Exponential with no delay
has nothing to double and is reported.

Stopping is **cooperative**, and it is worth knowing why. Python cannot kill a
thread, so a timeout sets the step's cancel token and every built-in plugin
either waits on that token or passes the remaining time down to whatever it is
actually waiting on. A command is signalled and then killed; a service request
gives up; a scenario run is interrupted the way the GUI interrupts one. A plugin
that ignored its token would run to completion, and the run would say so on its
way out rather than hanging in silence.

**Past its timeout, the person watching decides.** When the run has the
application to ask through, a step that outlasts its `timeout` is not stopped
at once: a window asks *Continue* or *Cancel* while the step keeps working.
Continue gives it another `timeout`, after which it is asked again; Cancel, a
closed window or no answer within ten minutes stops it as a timeout. A step that
finishes while the window is up takes the window away with it. With nobody to
ask - a run from the command line - the timeout stops the step as before. An
approval gate, `time.wait` and the service steps are never asked about: their
timeout is part of what they do.

Ctrl+C is a cancellation, not a crash: the run still gets its cleanup pass, its
record and its verdict.

---

## Watching a run: Stages and artifacts

**Stages** is the run's account, one row per thing a step reported. It can
live in a window of its own: the mark at the right end of the tab strip (it
has no label - hover it for one) moves the feed, with the message box under it,
into a separate window that you can minimize, maximize or put on another
screen. The tab stays where it was and says where Stages went; **Show window**
brings the window forward, and the mark beside it - or closing the window -
puts Stages back in the tab. **View -> Stages in a separate window** does the
same.

The window lives only as long as the application: closing the main window
closes it too. Whether Stages is in its own window is remembered, with the
window's size and place, so it opens the same way next time.

**Ctrl+Shift+S** (View -> Go to Stages) goes to Stages from any window of the
application: it brings the Stages window forward - back from the taskbar if it
was minimized - or opens the Cycles page on the Stages tab, unfolding the
bottom panel if it had been dragged shut.

**The files a run wrote** are one click from the step that wrote them. Right-
click a step -> **Artifacts** lists its files and its folder; **Artifacts**
above the Output tab does the same for the selected step (the run's folder with
nothing selected, or when the step wrote nothing); and every run in the
Subjects list has an **artifacts** link. Each opens the Artifacts page on that run with the file
selected. These are offered only while a run of the cycle that is open is on
screen.

---

## Talking to a running cycle

Open **Stages** and write in the box under the feed. **To** shows who reads it,
and it is chosen for you from who is working:

- **One step is working** that takes clarifications: that step, marked
  **AUTO**. Only that execution of it reads the message; a plan revision that
  sends the step round again does not silently redirect it.
- **No step is working**: **Further work of this run** - every compatible step
  still to come reads it at its next supported point.
- **Two or more are working at once**: nothing is guessed. **To** asks you to
  choose; selecting one of them on the graph, or picking it from the list,
  settles it.

To choose yourself, pick any recipient from the **To** list: a working step, a
step marked **(not started)** - which reads it when it starts - or the rest of
the run. **Back to automatic** returns to following who is working.

The recipient never changes under a draft. It is fixed once you start typing,
or, if nobody could be named then, the moment somebody first can. New stages,
another step starting and a different node selected on the graph do not move
it. If the step it is for stops taking messages, the draft stays, says so, and
offers **Send to ... instead** in one click - it is never sent elsewhere on its
own. The plugin says when it will next read text, and the composer shows that
beside the recipient.

**Enter** sends; **Shift+Enter** starts a new line. There is no Send button.
**Ctrl+Shift+M** (Run -> Clarify the running cycle) opens the same composer
over whichever window of the application is in front, so you do not have to go
to Cycles -> Stages first. It keeps its draft when closed with **Esc**, closes
by itself once the run has saved the message, and stays open with the reason if
the run refuses it. A modal dialog keeps the keyboard to itself, as usual.

**What a step was told reaches the steps that judge its work.** A message to a
step is for that step, but a later step that reviews or accepts its work has to
know it - otherwise it judges work that followed you against the task as it
stood before, and sends it back for following you. So once a message has
**arrived** at a step, every step after it in the graph that takes
clarifications reads it too, at its next point, marked as said to that earlier
step. In the development cycles, a clarification sent to `implement` therefore
also reaches `review` and `accept` (and `probe` where there is one). A message
that never arrived is not handed on, a step running beside it does not get it,
and a plan revision that sends the step round again leaves it behind with the
old work. The message's row lists those later steps as "Also shown to".

For example, send **This method is also called from cron** to an implementation
step while its first call is running. The message waits for the next supported
call, where it is added to the original task as a separate clarification.
Sending text does not interrupt a running CLI call or force another iteration.
If the step finishes before reading it, the message says it was not delivered.

Messages appear in the stages feed with their recipient and delivery status.
**Sending** means the GUI wrote a request; sending again reuses the same ID, so
it can never become a second message. **Saved - waiting for delivery** means
the core saved it.
**Delivered** means the plugin confirmed which call received it; it does not
claim the suggestion was followed. **Rejected**, **Not delivered** and
**Uncertain** give the reason. A clarification for the whole run lists delivery
to individual steps: one reader does not consume it for everyone else.

The current agent adapters read before each external call. Review and edit
have one call; implementation has attempt and review boundaries. Once a known
last call begins, that step no longer accepts text. Other plugins can declare
the same capability without the core or GUI knowing they are agents.

Messages, snapshots of each call's input, and confirmations live in
`.execution/interactions.json`. Resume retains them and reconstructs the
same inputs for recorded calls. New clarifications apply at later supported
points; completed work remains completed. Start over and partial runs start
a new message history. Reading an old run shows saved messages and disables
sending, even if diagnostic stage events were lost.

### The control protocol

Run with `--events=- --control=-`. The event
`cycle.interaction.offers` publishes the current `version`, a `run` offer
and `steps` keyed by step ID. Each step offer includes `execution`,
`kinds`, `state` and `when`. Send one JSON line, using that run's ID and
the execution from the offer:

```json
{"command":"cycle.message","id":"note-1","run_id":"<run-id>","scope":"step","step":"implement","execution":1,"offer":3,"kind":"text","payload":{"text":"This method is also called from cron."}}
```

For the rest of the run use `"scope":"run"` and omit `step` and
`execution`. A `cycle.interaction` event carries the complete `message`
record, including its ID, state, reason and per-consumer `deliveries`.
`cycle.interaction.refused` identifies a request that could not be recorded.
Queueing a request is not an acceptance: wait for the record. Repeating the
same ID and content returns the existing record; reusing the ID with different
content is refused. The current limits are 8,000 characters per clarification
and 200 message records per run.

Only `text` is supported in this release. The format reserves `answer`
and `action`; those are rejected until an implementation handles them.
Approval answers still use `ask.result`, and stopping still uses
`cycle.stop`. Free text does not become a shell command.

### Adding message support to a plugin

Declare `accepts=(accepts("text", "Clarification", when="Before its next call"),)`
in `PluginMetadata`. During execution use `context.inbox(step.id)`:

1. Call `take(point, last=False)` with a stable point name, such as
   `"attempt-2"`. The returned snapshot is saved and immutable; taking the
   same point after Resume returns the same messages, including an empty set.
2. Include `snapshot.items` in the operation's real input so its receipt key
   reflects the clarification. Preserve the original task. Each item carries
   `scope`, `text`, `to` (the step it was addressed to) and `earlier` - true
   for a message said to a step upstream of this one, which the plugin should
   present as what that step's work was asked to follow.
3. Before a new external call, `bind(snapshot, relative_operation_directory)`.
   Record the operation before starting it and its response before confirming
   delivery. A known outcome from a process that could not start should call
   `Operation.complete(..., input_delivered=False)`.
4. Confirm with `delivered(snapshot, relative_operation_directory)` when
   there is evidence of delivery, or `release(snapshot, reason)` when the
   call never started. An unresolved external call remains uncertain.
5. Use `take(..., last=True)` before the known final call to withdraw the
   offer, and `offer(when)` to update the timing while another point remains.

The scheduler owns request acceptance and the run record. Snapshot and
confirmation writes go through the thread-safe inbox store on plugin workers.
The GUI and diagnostic observers never own accepted messages. See
[execution and recovery](cycle-recovery.md) for uncertain external outcomes.

---

## What a run leaves behind

```
~/QAVector/cycle-runs/20260917-193412-nightly/
    metadata.json        the whole run: every step, output and artifact
    graph.json           the graph as it was, so an old run still draws
    logs/cycle.jsonl     the event stream, replayable
    steps/<id>/          stdout.log, stderr.log, and whatever that step wrote
    artifacts/
    reports/             run.json, run.html
```

One directory, so it can be zipped and attached to a bug report. **Nothing in it
is an absolute path**: the directory describes itself, which is what lets it be
read on another machine.

`index.json` beside them lists the runs, newest first, each with its subject
and the step it got to - which is what the sessions are grouped from.

---

## Tags, and what is not here yet

Deliberately absent for now, so nobody looks for them: event sources other
than a watched step (see *Starting when a new task appears*), analytics
plugins, nested cycles, loops in the graph, and a plan that changes itself
during a run - see [the adaptive cycle design](cycle-adaptive-design.md) for
where that is going.

A wait lives **inside one run**: the process stays up for it, so it suits
minutes and hours rather than days. Suspending a run to disk and resuming it in
a later process is a different feature and is not here — though the record a run
already writes at every step would be most of what it needs.

Stopping is cooperative (see *Timeouts, retries and stopping*): a step is asked
to stop and every built-in plugin does, but nothing can kill a thread.

---

## Adding a plugin

Adding one touches **four** places, and the fourth is the one that is easy to
forget:

1. A module in `cycle/plugins/` — a class with a `metadata` (a
   `PluginMetadata`) and an `execute`. Declare every setting as a `field(...)`
   and every output as an `output(...)`: the step window's form and
   `problems()`'s validation both read that table, which is why they cannot
   disagree.
2. `cycle/plugins/__init__.py` — the class in the `BUILTIN` tuple. Nothing
   scans; the set is greppable on purpose.
3. `tests/test_cycle_plugins_<name>.py` — what it runs, what it reports, and
   what it does when its cancel token is set.
4. **Both build manifests** — `pyproject.toml`'s `packages.find` and
   `packaging/pyinstaller/core.spec`'s `collect_submodules` already cover
   `cycle*`, so a module inside the package is fine; a *new top-level package*
   is not, and would work in a checkout while being silently absent from every
   installed build.

A plugin that starts something and leaves it running subclasses `ManagedPlugin`
and implements `start` / `stop` / `status` instead of `execute`; the run holds
the handle and stops it at the end.

A plugin whose step is worth listening to - it reads a queue that new work
arrives in - sets `watchable=True` in its metadata and implements
`peek(settings)`: what the step would take now, as a list of `{"key", "title"}`,
without running anything or writing anywhere. That is all a cycle's `triggers:`
needs; the engine and the application only compare keys, and never learn what
kind of queue it is.

A plugin that has nothing to do *yet* returns `registry.waiting(seconds)`
instead of a verdict. It keeps no state between turns — each one is a fresh
call — so "how long have I been at this" is answered from the run record with
`context.started_at(step.id)`, which the executor preserves across waits.

Plugins are **blocking**, not async. There is no event loop anywhere in this
application, Playwright's sync API is bound to the thread that created it, and
the service bridge blocks on an event — so the executor runs plugins on threads.
`cycle/registry.py` says this at greater length, because it is the kind of thing
somebody tidies back into `async def` without realising what it would cost.

Permissions in a plugin's metadata are **declared, not enforced**. They exist so
that the day there is a sandbox, every plugin already answers the question. A
permission list is documentation today, and must never be read as a guarantee.


### Asking for a revised plan

For an approval that should return human feedback to the planning agent:

```yaml
- id: approve
  plugin: approval.gate
  needs: [recheck, settle]
  with:
    question: Start work on this plan?
    detail: ${steps.settle.outputs.summary}
    revision_step: settle
    max_revisions: 3
    options: [Start work, Leave it]
```

`settle` must be an upstream `agent.review` node; intermediate nodes can only
review or check. **Send for revision** repeats that path with the feedback, then
shows approval again. It never means approval, and it does not restart earlier
completed steps. Agent calls for a new revision can incur charges. See
[execution and recovery](cycle-recovery.md#human-feedback-and-plan-revisions).
