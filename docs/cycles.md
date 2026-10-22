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

A cycle can hold its steps to its project folder - network, files and limits,
with each step able to set its own. Shell, git, agent, Jira and Teams steps are
held; a browser step (`scenario.run`) and any step marked `system: true` run
without them, and the node says SYSTEM. See [restrictions](restrictions.md).

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

**Saved steps.** A step that took a while to set up - a Jira site, an
account, a status - is kept with right-click, **Save as preset...**, under a
name; it works on any cycle, including one that ships with the application.
It then sits under **Step → Saved** in every cycle, whatever project it is
in, grouped by the kind of step under the plugin's name - Jira Issues, Shell
command - and within a kind in the order it was saved. It adds a copy of
itself after the selected step, its id taken from the name. A saved
step keeps its settings and label, not its place: no id, no `needs`, no phase,
and no condition that reads another step. The variables it refers to come
with it and are added to a cycle that lacks them. A secret is kept in the
saved step as `{secret: true}`, never its value: the step remembers the cycle
it was saved from, and adding it copies that cycle's value inside the secrets
store (`--cycle-secret-copy=FROM:TO:jira_token`), unless the cycle already
has one of its own.
Saving under a name already kept replaces it; **Saved → Remove** forgets one.
The list is `cyclepresets.json`, beside `cycleprojects.json`.

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

**A link that fixes what a step reads.** A link is order - its `needs:` - and
a `${steps.<id>.outputs...}` in a step's settings is data; drawing one never
changes the other. A step brought from another cycle often still reads its old
neighbour by the id it had there. So when a new link (drawn, moved, or ticked
under **Waits for**) makes a step wait for another while it reads a step this
cycle does not have, the canvas asks whether to read it from the step it now
waits for; yes rewrites those references in its settings, and one Undo takes
back the link and the rewrite together.

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

**Design and Run.** Design shows the file or unsaved changes. Starting a cycle
switches to **Run → Cockpit**, the operational summary. **Run → Graph** shows
the graph the run started with and its statuses. A file edited afterwards has
its own Design graph; an edit switches back to Design. Which one a cycle was
left in - and Cockpit or Graph within Run - is remembered per cycle: opening it
again, or after a restart, shows its last run the same way. Without a run of it
to show, it opens in Design.

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
| `agent.merge` | Lets an agent merge branches and resolve conflicts in a repository with no remote. Local git only, from a fixed list; it cannot push. Reports every command it ran. |
| `git.commit` | Commits the tree that was verified, and refuses anything else. Local only — no push. |
| `jira.issues` | Reads a person's Jira issues and the comments on them. Reads only. |
| `jira.transition` | Moves one issue along its workflow. Writes — and only this. |
| `memory.recall` | What earlier runs remembered about a key. Reads only. |
| `memory.remember` | Writes something into a key's record, or moves a counter. |
| `memory.claim` | Takes a task for this run; given back when the run ends. |
| `approval.gate` | Stops and asks a person. Anything but an answer fails the step. |
| `user.input` | Stops and asks a person to write something; later steps read it as `outputs.text`. |
| `user.choice` | Stops and asks a person to pick from a list an earlier step made; later steps read `outputs.values`. |
| `check.gate` | Several things that must all hold. Names the one that did not. |
| `report.json` | Writes a JSON snapshot; a separate final report follows the terminal verdict. |
| `report.html` | Writes an HTML snapshot with a link to the final execution result. |

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

### Coming back to a subject: the Subjects page

A session is one cycle's runs on one subject. The work is wider than that: a
task is planned and built by one cycle, released by another, and somebody asks
a single-agent cycle about it in between. **A subject** is the thing itself -
every run, of any cycle, whose subject has that key - and **Subjects** on the
rail lists one row each: what waits for you first (a question asked, a node
waiting for consent, a pause, a run you parked, an approval nobody answered),
then what is going, then the rest. *Needs you*, *Active* and *All* narrow it;
Ctrl+K finds a subject by key, title, cycle or note.

A row opens in place, across the whole list, and any number may be open at
once - what is open is remembered. An open row says what to do next as one
button: **Answer in Cockpit** for a question the run in this window is asking,
**Resume** for a run that stopped where it can go on (the row says why when
Resume would be refused - a cycle edited since, say), **Open run** otherwise.
Under that are your note and the subject's story:

- **History** - every run of every cycle that named it, oldest first, the way a
  conversation reads: how each run began and ended, its pauses, every question
  it asked with what was shown and what you answered (a plan sent back
  included), every message you sent a step, every agent's answer, your notes.
  An agent's entry unfolds its full answer, opens the prompt it was given, and
  shows its own account - what it weighed, said and reached for, call by call,
  without what its tools returned. *All* adds the checks, the failures and the
  steps that simply passed. It opens on the newest entry and stays there until
  you scroll up.
- **Files** - each run's step files, newest run first; a click opens one on the
  Artifacts page.
- **Runs** - every run; a click puts it on the Cycles page.
- **Stages** - one run's stages, picked from the subject's runs. The run going
  in this window shows the live feed with the box for writing to it - the
  Cycles page's own Stages panel, moved into the row while it is on screen and
  back in its tab the moment you leave, never a copy. Stages in a window of
  their own stay there; the row says so and brings the window forward. Any
  other run shows the stages it recorded.

A run can say a great deal, and a row has a share of the list to say it in. The
mark at the end of an open row's top line puts its section in **a window of its
own** - on another screen, as large as you like, minimized by itself - opening
on the tab the row was showing, live Stages included. The row says where it
went, with **Show window** and the way back. Closing the window, or the mark
inside it, puts the section back in its row; closing the row closes its window
too. The windows go with the application.

Nothing there is stored twice: the history is read from the runs whenever it is
shown. Two things a run did not keep before are kept now, so the history can
say what was asked and not only what came back: a question step writes each
time it asks - question, detail, options, answer, who - to
`steps/<id>/asked.json`, and an agent call writes the prompt it was given,
clarifications included, as `prompt.md` beside its output. Older runs show the
answer and say that the question was not recorded.

**Notes** are the one thing kept for a subject: what you write about it -
where you stopped, what you are waiting for. They are added, never rewritten,
kept in `subjectnotes.json` beside the cycles' memory, and shown in the row and
in the history.

**Park** frees the window. It holds one run at a time, so a run waiting at a
question - or at a phase boundary for hours - would keep every other subject
from starting. Park pauses the run, waits until only questions to a person are
left going (an agent mid-call finishes that call), and stops it as *parked*:
everything finished stays finished, and Resume, from the row, asks the open
questions again. **Run a cycle on it...** starts any cycle on the subject: one
that names its own subject takes the key through its `pin`; one that names
nothing is told what the run is about (`--cycle-subject`). Either way what it
asks and answers joins this history.

From a terminal: `--cycle-subject-list` (with `-view=`, `-limit=`, `-offset=`),
`--cycle-subject-history=KEY` (with `-scope=conversation|all`, `-limit=`),
`--cycle-subject-transcript=RUN:STEP` (an agent step's account), and
`--cycle-subject-note=KEY --cycle-subject-note-text=TEXT`. A control line
`{"command": "cycle.stop", "run_id": ..., "reason": "parked"}` stops a run with
that reason as its message.

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

**One task, or everything the step returns.** What is offered is what a run
started from the offer would take, and the watched step decides that - the
listener adds no selection of its own. When the trigger's `pin` is one of the
step's inputs (`issue: ${vars.task_key}`, as above), a run takes the one task it
is started on, so each new task is its own offer. When the step does not read
the pin - a release that takes every task ready for it - a run takes the step's
whole answer, so that is one offer: *Start Ready To Release on 3 tasks?*, with
every task listed, made whenever one of them is new. The step's own `limit`
holds. Start runs the cycle with `pin` set to the keys joined by commas; Ignore
and Start mark each of them seen. Planned, it is one entry - *3 tasks  QA-1,
QA-2, QA-3* - replaced by the next answer if that is planned too, since the
latest answer is what a run would take now.
Started, the run keeps that name in Subjects: a cycle that names no `subject:`
of its own is about what it was started on.

```yaml
variables:
  offered_task: ""     # where the offered keys go; the step does not read it
triggers:
  - id: ready_for_release
    watch: ready        # role: everyone, jql: status = "Ready for release"
    pin: offered_task
```

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

### Starting something on the plan later

Double-click an entry on the plan - a planned task, or an approval nobody
answered - and a window asks when: **Start now**, or a day and a time and
**Schedule**. The day is today or one of the three after it: a start can be
put at most three days ahead. A scheduled entry says so in the list -
*scheduled for 09 Oct 18:00* - and double-clicking it again moves the time or
takes it off (**Clear schedule**).

At that time, if the application is open, the entry starts as though Start
work had been pressed - after the run going on then, if there is one. If the
application was closed at the time (or the machine asleep), it asks when it is
next open: *Start it now?* - and **Not now** keeps the entry on the plan
without a time. A start more than three days in the past is never made nor
asked about: the application may be opened a month later, and what was planned
a month ago is not today's work. Such an entry stays on the plan, saying it
will not run, until somebody starts or removes it.

A whole-answer entry (a release) keeps its time when the next answer replaces
it: a task that turns up at three does not cancel the release scheduled for
six. From a terminal: `--cycle-plan-schedule=ID@SECONDS` sets the time
(seconds since the epoch), `ID@` takes it off.

### How a run began

Right after `cycle.run.start` every run says how it began, as `cycle.run.mode`:

| `mode` | What it means |
|---|---|
| `fresh` | Nothing taken from anywhere. |
| `resume` | The same run again: `kept` lists what it took from its last attempt, `rerun` what it does again, each with its reason - *failed last time*, *waits on a step that runs again*, *asks again on every run*... |
| `partial` | Run step / Run from here: `kept` is what was borrowed from `source_run`, `rerun` what was chosen. |

A plan sent back from an approval is a `cycle.revision` event with its round,
the feedback, and the steps it revisits. The Subjects pane shows all three.

### What a run completed

Technical `status: success` and exit code 0 still mean that the selected
execution succeeded. They do not mean that a partial run completed the whole
workflow. Schema 1 adds independent presentation fields to metadata, index
rows, sessions and `cycle.run.start` / `cycle.run.end` events:

```json
{
  "status": "success",
  "execution_status": "completed",
  "scope": "partial",
  "coverage": {"completed": 1, "selected": 1, "total": 68},
  "outcome": "partial_completed",
  "current_phase": "Prepare"
}
```

`scope` is `full`, `partial` or `resume`. `selected_steps` records the effective
selection, including any gates that must run again. Coverage counts successful
selected steps, excludes borrowed results in a partial run, and includes
retained successful work on Resume. Intentional skips are not successful steps,
so a completed full workflow can have fewer successful steps than its total.

Resume preserves the original selection for named actions, Run step and Run from
here. Changing `scope` to `resume` never expands the selected work. Legacy
partial selections are inferred conservatively from borrowed and not-selected
step receipts.

Subjects and Search show neutral **Partial run completed** and **No work
found**, rather than Passed. A full successful execution with no unresolved
work reads **Workflow completed**. Unavailable outputs or unexplained skips
read **Workflow incomplete**, even if the technical verdict is success. No
selected work reads **No work performed**. An empty subject alone never proves
that nothing was found: `no_work_found` requires successful empty `jira.issues`
reads with no other executed work. Cycle-specific milestones come later.

With explicit phases, `current_phase` identifies the execution frontier or the
completed boundary at Pause. Legacy cycles use the reached step's descriptive
`group`, which does not change scheduling. Skipped receipts and events
carry `reason_code`: `not_selected`, `condition_false`, `disabled`, `blocked`,
`rejected`, `unavailable` or `plugin_skipped`. `blocked_by` names a causal
dependency when known. Legacy messages are interpreted conservatively; an
unknown skip remains `plugin_skipped`.

Old metadata and events remain readable without rewriting run directories.
Sessions infer missing fields from saved steps and the run's graph; legacy
JSONL replay uses its run mode and step evidence. When evidence is unavailable,
the GUI uses neutral **Execution completed**. A new core's final outcome takes
precedence over missing diagnostic step events.

Baseline reports export only the fixed execution summary fields. Generated
modern fixtures use container schema 2, preserving sanitized selection,
phase/milestone and approval-state evidence; checked-in historical schema-1
fixtures remain unchanged. Export verifies that reloading each sample produces
the same execution summary. Task text, paths, settings, raw approval receipts
and absolute identifiers/clocks are excluded.

### Prepare task and bounded run actions

A cycle can declare a named selection that runs fresh, without borrowing
results from another run:

```yaml
actions:
  prepare_task:
    title: Prepare task
    description: Recreates the task sandbox; committed source branch work is reused.
    steps: [todo, claim, workspace, task_brief]
    result: task_brief
```

The listed nodes must exist and supply their own inputs. A dependency outside
the selection is allowed only at a conditional join that does not read that
unselected node. Core rejects known agents, baseline/scenario plugins, external
write plugins and memory counter writes in `prepare_task`. Shell commands
remain declared operations: this does not enforce arbitrary shell effects.

**Run ▾ → Prepare task** appears when the open cycle declares that action. Its
confirmation explains sandbox recreation and reuse before work begins. From a
terminal, use `--cycle-run=claim_development --cycle-action=prepare_task`.
Selection/reuse/Resume flags cannot be combined with a new named action.

The run stores `action_id`, its launch-time `action_definition`, and explicit
`selected_steps`. The scheduler never submits nodes outside that selection,
including on Resume. Changing the action's selection requires a new run.
Successful Prepare has `scope: partial`, `outcome: task_prepared` and the neutral
label **Task prepared · Prepare only**. An empty Jira queue is **No work found**.

The `task.brief` plugin takes `issues` (exactly one task), `directory`, `source`
and `base`, plus optional `base_update` and `task_report`. It reads the actual
Git state and writes `task-brief.json` and `task-brief.html` in its step folder.
Subjects shows the task, Jira status, sandbox/source paths, creation or
recreation, branch reuse, HEAD and base SHA. A missing base merge, dirty
workspace or unresolved conflict prevents `task_prepared`. A failed Prepare
can still carry the blocked brief.

External effects read **None declared**, rather than claiming verified receipts
for arbitrary shell commands. The action excludes Jira transitions and pushes;
the general effect ledger belongs to a later increment. Git fetch updates
local remote-tracking refs, and preparing the sandbox can create local merge
commits. The source checkout's working files are preserved.

The Claim upgrade moves its existing `attempts` increment behind preflight and
baseline, directly before implementation. It preserves the counter's value and
the existing admission limit; a task already at its limit is still refused.
Prepare never increments it. A new Prepare recreates the disposable sandbox,
so the confirmation explicitly covers replacement of its uncommitted work.

The working Claim cycle lives in user data, and so does its upgrade. Create a
fresh draft against that user's current file with:

```bash
.venv/bin/python -m cycle.prepare_claim \
  --source /path/to/cycles/claim_development.yaml \
  --output /tmp/claim_development-prepare-task.yaml
```

After review, `--apply --expected-digest=HASH` writes back only if the source
still matches the printed digest, preserving comments and file permissions.
Use the updated core and GUI with this schema. Old runs still open, but the
structurally changed graph cannot Resume executions of the old graph; start a
new Prepare run instead. Unmodified cycles and their recovery behavior keep
their existing semantics.

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
step. A gate that approved when it could not ask would stop being a gate
exactly where somebody believed they had one.

**Unless the step names the answer to take.** `auto_answer:` is one of the
answers, taken by itself when nobody has answered within `auto_after:` seconds
(300 unless said otherwise, and always under the step's own timeout). The
window says so and counts it down - *"Put it into claim/" automatically in
4:59* - the result says `approved automatically`, and the step publishes
`automatic: true`. Closing the window by hand is still no answer, and a
stopped run is still stopped. Blank means never, which is what makes it safe
to point at an earlier step: a check that prints the answer only when every
review said ready leaves the gate an ordinary one whenever a person has to
decide.

```yaml
  - id: take_release
    plugin: approval.gate
    needs: [release_is_ready]
    timeout: 3600
    with:
      question: Put the release into the project?
      options: [Put it into the project, Stop]
      auto_answer: ${steps.release_is_ready.outputs.stdout_tail}   # blank unless ready
      auto_after: 300
```

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

### Asking a person to write something

`user.input` asks for words rather than a decision - the task an agent is to
do, a branch name, a note - and hands them on as `${steps.<id>.outputs.text}`.

```yaml
  - id: task
    plugin: user.input
    timeout: 1800
    with:
      question: What should the agent do?
      placeholder: Describe the task - what to change and how to tell it is done.

  - id: implement
    plugin: agent.implement
    needs: [task]
    with:
      task: Do the task in inputs.task.
      inputs:
        task: ${steps.task.outputs.text}
```

The window offers **Send** and **Cancel**. It keeps the gate's rule about
silence: Cancel, a closed window, the deadline passing or no application to
ask in all fail the step, and so does sending an empty box unless
`required: false`. `default:` puts text in the box to start from. Unlike a
gate, what it collected is reused: resuming a run or running part of a cycle
takes the text written last time instead of asking again.

### Asking a person to choose

`user.choice` shows a list an earlier step made as a table and hands on what
was ticked. A queue of tasks is the usual list: a lighter step reads the queue,
the choice is made, and a third step reads the one chosen in full - so every
step after it keeps reading `todo` as it always did.

```yaml
  - id: queue
    plugin: jira.issues
    with:
      status: To Do, REOPEN
      issue: ${vars.task_key}       # a run started on one task lists just that one
      limit: 50
      comments: false
      attachments: none

  - id: pick
    plugin: user.choice
    needs: [queue]
    timeout: 3600
    with:
      question: Which task should this run take?
      items: ${steps.queue.outputs.issues}
      value: key
      columns: [key, summary, status, assignee]
      item_detail: "{summary}\n\n{description}"
      mode: single
      choose: ${vars.task_key}       # ...and takes it without asking
      when_empty: skip

  - id: todo
    plugin: jira.issues
    needs: [pick]
    with:
      issue: ${steps.pick.outputs.value}
```

`mode: single` takes exactly one row, `multiple` one or more. `checked` is what
is ticked when the window opens: `all`, `none`, or values such as `QA-1, QA-4`.
The outputs are `values` and `items` (in the list's order, which the table
keeps), `value` and `item` (the first), `joined` (the values with commas, for a
shell step) and `count`.

**What it does when the data is not there.** An empty list never opens a
window; `when_empty` decides: `skip` passes the step and everything that needs
it, `continue` succeeds with nothing chosen, `fail` stops the run. A `choose`
value the list does not hold fails the step, empty list or not - a run started
on a task that is not there is an error, not an absence of work. So does an
item with no `value`, two items with the same one, a mixture of mappings and
plain values, and a column no item has; each says which. In the window, an
empty cell is a dash, a row with no detail says so, and a search that hides
every row says that instead of showing an empty table.

As with a gate, Cancel, a closed window, the deadline or no application to ask
in all fail the step. What was chosen is reused by Resume and by a partial
run.
### Merging with an agent

`agent.merge` is for the one job `agent.edit` cannot do: the work is `git
merge`, and a conflict is resolved by editing files and then `git add` and
`git commit`. It is given the editing tools and a fixed list of local git
commands - status, diff, log, show, `merge --no-ff`, `merge --abort`, add, rm,
commit, `checkout --ours/--theirs`, `reset --hard`, reflog and a few reads.
The Claude Code CLI also lets through on its own the commands it judges
read-only - `grep`, `head`, `ls` - which cannot write or reach anything.
Push, fetch, pull, remote, clone and config are refused by name.

It also refuses to start in a repository that has a remote, and fails if one
appears during the step: a list of refused commands is a promise about the
CLI, a repository with nowhere to push to is a fact about the disk. Make a
scratch clone for it in a `command.shell` step and remove its remote:

```yaml
  - id: prepare
    plugin: command.shell
    with:
      command: |-
        git clone --quiet --shared --no-checkout "$SRC" work
        cd work
        git fetch --quiet "$SRC" '+refs/remotes/origin/*:refs/remotes/up/*'
        git remote remove origin
        git switch -c release --no-track up/production-ecs
  - id: merge
    plugin: agent.merge
    needs: [prepare]
    with:
      directory: work
      model: opus
      task: Merge each task's branch with --no-ff, in order ...
```

Every command the agent ran comes back as `commands`, in order - an attempt
the CLI refused is in `refused` instead, since nothing it would have done
happened - with
`branch` and `merging` (1 when a merge was left unfinished). A review after it
can then check the work against the plan - the order of the merges, a push
attempted - rather than against the agent's own account. Taking the result out
of the scratch clone is a step of its own, after somebody has said yes.

### A review's verdict

Whether the work can go out at all is a different question from what is
wrong with it: a release can be unshippable for a reason no step in the cycle
is allowed to fix - a bug in a task's own code, a migration Odoo will skip.
`verdict: true` asks for that answer as well and publishes it as `ready`
(true or false). It also makes every issue say whether a later step can fix
it - `fixable: true` for something the reviewed work did wrong, `false` for a
problem only an outside author can change - and `fixable_count` counts the
blocking ones a step can fix. A fix round waits on that count, so a problem in
a task's own code goes to a person instead of to an agent that can do nothing
about it:

```yaml
  - id: fix_round
    plugin: agent.merge
    needs: [review]
    if: ${steps.review.outputs.fixable_count}
  - id: ask_about_tasks
    plugin: approval.gate
    needs: [review]
    if: ${steps.review.outputs.ready} == 'false'
```

Without `verdict` an issue says nothing about it, and `fixable_count` is the
same as `blocking_count`.

### Telling Teams

`teams.message` posts a message to a Teams group chat, channel or person
through a **Workflows webhook**. In Teams, the Workflows app's *Post to a chat
when a webhook request is received* (or *... to a channel*), pointed at the
chat it should post in, gives a URL. That URL is the whole of the permission,
so it goes in a secret variable:

```yaml
variables:
  teams_release_webhook: {secret: true}
steps:
  - id: notify_team
    plugin: teams.message
    needs: [release_note]
    with:
      webhook: ${vars.teams_release_webhook}
      text: ${steps.release_note.outputs.summary}
```

The text is laid out as a card: each line a block, `- ` bullets drawn as
bullets, `**bold**` left to Teams. `title` adds a heading. A message longer than
the forty lines a step keeps of its output - every test that failed, a line
each - is written to a file by the step before and sent with `text_file:`
(in the run workspace, or a whole path) instead of `text:`. The URL is never
repeated - not in the step's message, its outputs or an error, which name only
the host and the HTTP status. Only `https://` is accepted, or `http://` to a
stand-in on this machine. Running the step again posts again; resuming a run
does not re-run a step that finished.

Leave `if:` off a message that sends another step's work. A step with an `if:`
runs even when a step it needs was skipped - that is what lets a condition
react to a skip - so `if: ${vars.teams_release_webhook}` sends after a
release note that was never written, and fails reading it. Without one, the
message is skipped with the step it needs.

`mentions:` names who Teams should notify - one per line or comma, as
`name <email>` or the email alone, their Teams sign-in. Typing "@name" in the
text notifies nobody: a card mentions somebody only when its text holds
`<at>name</at>` and the card lists that tag with their address, so the step
does both. Where the text already says `<at>name</at>` - beside the test a
person's change broke - the mention is made there; anybody else is tagged on a
line of their own at the end. The list usually comes from a step before
(`mentions: ${steps.who_broke.outputs.stdout_tail}`); an entry it got wrong is
named in the step's message and left out, rather than costing everybody the
message. `mentioned` says how many were tagged.

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

**It shows on the canvas.** The link from the step a condition reads into the
step it decides is drawn dashed, with the condition in a few words on it -
"if files changed", "unless rejected", "either way" - and the expression itself
in the link's tooltip. A condition that reads none of the steps it waits for
marks every link in.

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

## Execution phases and Pause

Explicit phases are execution boundaries. `group:` remains a visual grouping
for cycles without them; it grants no permission to start work. A phased cycle
assigns every step to a declared phase, and dependencies can only point within
the phase or to earlier phases:

```yaml
phases:
  - id: prepare
    title: Prepare task
    purpose: Read the task and save its workspace evidence.
    policy: stop_after
    outcome: task_prepared
    deliverables: [brief]
  - id: implement
    title: Implement
    policy: manual
steps:
  - id: brief
    plugin: task.brief
    phase: prepare
    # with: ... the normal task.brief settings
  - id: implement
    plugin: agent.implement
    phase: implement
    needs: [brief]
    # with: ... the normal agent.implement settings
```

Phase policies are:

| Policy | Scheduler behavior |
|---|---|
| `automatic` | Continue when the selected phase members settle successfully. |
| `stop_after` | Pause before starting the next phase after a successful phase. |
| `manual` | Wait for **Run phase** before starting this phase. |
| `approval_required` | Require `approval: gate_id`, naming an existing `approval.gate` inside this phase. Every other member must depend on it; the core also blocks execution until that gate succeeds. |

`approval_required` uses the gate's existing question and answer. Continue and
Run phase do not provide an approval answer. Named actions remain bounded by
their declared step selection; normal phase-boundary pauses are not added to
**Prepare task**.

**Pause** immediately bars new submissions, lets active steps reach their
cooperative completion or plugin waiting point, then shows **Paused**. Pending
steps stay pending. Plugin waiting and run Pause are different: plugin waiting
normally consumes its timeout, while a paused run freezes active-time accounting
and parked step deadlines. Active atomic operations can take time to drain.

**Continue** proceeds in the same live executor and run identity, retaining
successful results. **Run phase** runs the next phase and pauses before the
following one; completed or future phases cannot be selected to skip a boundary.
**Retry phase** is offered for a failed current phase. It retains successful
work and retries its unfinished nodes with new execution generations and a
recorded control transition. It refuses to invalidate successful downstream
work, repeat an unresolved operation, or restart a failed step that still holds
a managed resource. Retrying verification keeps its successful implementation
and counter. Retrying a failed implementation rechecks its existing memory
budget and charges its direct counter prerequisite again. Arbitrary counters
elsewhere in a graph are not inferred to be implementation budgets.

**Stop** cancels active operations, marks pending work cancelled and runs normal
resource cleanup. It does not roll back completed effects. A live pause keeps
managed resources; before continuation the scheduler renews held task claims
and refuses further work if another run now owns a claim.

The existing Run control becomes Pause while executing, then Continue,
Run phase or Retry phase according to the core's available action. When both
Continue and Run phase are available, Run phase is in its dropdown. Stop stays
beside this control. The global activity strip opens the current run when
clicked and does not duplicate its buttons. A paused live
run keeps its process; **Resume** is for an execution whose process has ended.
Without a live control channel, the CLI exports a safe pause, saves pending
work and exits with code 0 (`status: waiting`, `execution_status: paused`). Use
normal Resume to open that saved execution again. Resume verifies the original
phase contract and restores process-local resources and approvals. Adding
phases to an existing cycle therefore requires a new run for that new contract.

An explicit **Run phase** or **Retry phase** records its phase bound before
submission. The bound survives Resume, and a fully skipped/no-op phase still
stops before the next phase. Skipped approval branches can settle as skipped;
executing their write nodes still requires a successful gate.

Each phase writes `.execution/phases/<id>.json` before the next phase can start.
Receipts reference individual step receipts, input/definition fingerprints,
execution generations and deliverable artifact hashes. `deliverables` names
producer steps in that phase; raw outputs remain in those steps' receipts.
Only a complete successful phase can claim its declared milestone. Updated
receipts retain history; reset step evidence invalidates an older receipt.
They survive process termination and a missing metadata update. `effects` is
only a list of declared descriptions at this stage, not a verified change ledger.

`python -m cycle.phase_claim --source FILE --output DRAFT` prepares six phases
for the reviewed 69-step Claim cycle: Prepare, Plan, Implement, Integrate,
Publish and Verify. The first five pause before the next phase. The migration
preserves commands, dependencies, step IDs, existing approval gates and task
counters. Applying uses `--apply --expected-digest HASH` from the reviewed
source. Full Run keeps the configured production-conflict agent in Prepare;
the separate 13-step **Prepare task** action still makes no agent calls.

## Execution modes and node approvals

The mode selector beside **Run** offers **Auto**, **Important** and **Approve**.
The selected mode is enforced by the core scheduler before submitting a node,
including when the cycle is launched through the CLI:

```yaml
execution:
  default_mode: important
steps:
  - id: publish
    plugin: git.push
    important: true
    importance_reason: Updates the development branch used by other developers.
    # with: ... the normal git.push settings
```

| Mode | Additional approval before node execution |
|---|---|
| Auto | None, including for important nodes. |
| Important | Every node with `important: true`. |
| Approve | Every executable node. |

An explicit `approval.gate` always asks its own question and gets no additional
system prompt. Disabled nodes and branches skipped by their condition or
dependencies get no approval request. Old cycles default to Auto. `important`
must be a boolean; a missing `importance_reason` is a warning, not a validation
failure. The node editor exposes both fields, and the graph marks important
nodes with `!` as well as showing their reason in details and preflight.

The Cockpit and global strip keep the effective mode visible. Auto explicitly
explains that important nodes also run without implicit approval. Pending
approval cards show the node, reason and a readable summary of the captured
inputs. Jira searches show the site, project, filters, ordering and included
content; a single pinned issue hides the inactive queue filters. Authentication,
download limits, execution numbers, timeout, capabilities and the bounded JSON
preview are in collapsed **Technical details**. Opening details does not answer
the request. Details stay open across refreshes for that request and reset when
the run or input fingerprint changes. Secret-bearing preview values remain
redacted. Declared phase effects are labelled **Declared changes**; absent effect
evidence reads **Effects not recorded** in the details until the change ledger
is implemented.

Approve and Cancel are native application buttons, separate from the HTML
preview. Cancel declines this node approval and safely pauses; it does not stop
the entire run. The action area stays inside Cockpit and scrolls for parallel
requests without expanding the form.

With keyboard focus in Cockpit, **Enter** (including the numeric keypad) presses
Approve and **Esc** presses Cancel. A single request is the keyboard target;
with parallel requests, Tab to that request's buttons first. Held keys do not
repeat decisions, and the shortcuts do not act on other pages or input fields.

Each card approves only its own node. Parallel branches can have separate cards
and continue independently. Awaiting consent consumes no worker slot and leaves
the node pending. Cancel records a declined decision and safely pauses the run with
`approval_declined`; it does not report a technical failure or silently skip the
node. Continue asks again. Stop uses the normal cancellation and cleanup path.

The core binds consent to run ID, node ID, execution generation, submission
attempt, resolved-input and definition fingerprints, mode and policy version.
It writes the request before announcing it and the decision before submitting
work. Changed inputs require a new decision. One grant covers the node's
configured retry policy and unchanged plugin waiting polls; a poll with changed
inputs needs new consent in a protected mode. Explicit Retry or failed Resume
also needs a new grant. Successful retained nodes are not asked again.

`initial_execution_mode` is part of the immutable launch manifest.
`execution_mode` is the effective mode. Changing the YAML default cannot change
a saved run's policy. During a live run, mode changes are allowed only after
safe Pause and create a policy-change receipt; they invalidate pending and
approved but unstarted requests. The selector does not implicitly Continue.
Already-started unchanged waiting operations can finish their existing logical
execution. The Cockpit lists mode changes and node decisions.

The canonical `.execution/execution-policy.json` journal contains the mode,
request/decision receipts and policy changes. Checkpoint loading and GUI replay
read it independently of metadata, so a summary write lost at termination does
not lose a decision. Resume may reuse an approved but unstarted node only in
the same run with the same binding. An unanswered headless run exports a safe
pause rather than starting work without consent.

Use `--cycle-mode=auto|important|approve` to select a mode at launch. Omit it on
Resume to keep the saved mode; an explicit changed mode creates a recorded
resume policy change. The control pipe accepts `cycle.node.approval.answer`
with `run_id`, `request_id`, `fingerprint` and boolean `approved`, and
`cycle.mode.change` with `run_id` and `mode`. Answers are judged on the scheduler
thread. Stale answers and mode changes before Pause are refused.

---

## Watching a run: Stages and artifacts

**Run Cockpit** opens when a run starts. **Current step** is a prominent card
pinned above the scrolling history, with the node's name, status, elapsed time,
progress and last activity. It stays visible while reading earlier results;
new activity does not force the history to scroll. Its progress bar appears
only when the step reports a real current/total count. Pending approvals show
the node awaiting a decision; after Cancel that node is labelled Paused.
With parallel requests, focusing a request's buttons also selects its current
card. Clicking the card opens the step's details. Also running shows the other
active steps, their progress, last activity and remaining timeout. Next
shows the dependency frontier; conditions are still evaluated by the core
before a step starts. Completed lists results and their artifact counts.
Needs attention follows each blocked step to its original failure. Folded
graph phases show root failure and downstream blocker counts too.
Skipped nodes are omitted from Cockpit cards; blocked counts still lead to the
original failure. After Stop, Next no longer lists pending work: Cockpit shows
Stopping while active work finishes, then Stopped. The core still settles every
remaining node, and Graph and the journal retain their full statuses.
This also covers a declined explicit approval gate with `on_failure: stop`,
which ends as a failed run in the core even without pressing the toolbar's Stop.
Cockpit preserves that final status and refusal reason. A recoverable phase
failure or `on_failure: continue` keeps the remaining work visible.

Run status shares the existing cycle-title row, using a muted label; it adds
no row to the form. The mode stays in its selector. An empty result reads
`No work found`, without a repeated completion message or a zero timer.
Definition details and mode remain in the status tooltip.

The cycle header reads `Baseline tests 08:41, Running · +2 parallel`. A waiting
decision takes priority, then the active step selected by the user, then the
oldest active step. Progress does not change that selection. Clicking a step opens its
details. A strip below the main toolbar keeps the subject, phase, active step,
elapsed time and last activity visible away from the current cycle. It is hidden
while that cycle is already open, so it adds no duplicate row there. Click the strip to open
the current run in Cockpit, or use the Cockpit tab on the Cycles page.
Graph and the raw Output remain available.

Timers use `MM:SS`, `H:MM:SS`, then `Dd HH:MM`. The core records
`active_elapsed_ms`, `execution_generation` and bounded `execution_segments`
in metadata and active step receipts every five seconds and at transitions.
The GUI interpolates only within that five-second confirmation window; its
one-second repaint writes no events or checkpoints. Replayed history is frozen
at the last confirmed receipt. Legacy journals say **time unavailable** when
they contain no supervised-time evidence.

Plugin-owned waiting counts as execution time. Resume of an unchanged failed
or interrupted node continues its accumulated time. A changed definition,
refreshed successful gate or plan revision starts a new execution generation
and retains the earlier segments. Large scheduler/clock gaps and missing GUI
confirmations split segments; offline and suspend intervals are excluded, with
at most a five-second confirmation uncertainty. CLI runs use scheduler liveness
instead of GUI confirmations. Closing the control pipe cancels the run,
including when it closes before the scheduler starts.

`cycle.supervision` is diagnostic timing evidence in this increment. The
scheduler lease and complete supervision policy are a later stage. Phase Pause
and Auto/Important/Approve are available as described above.
External changes read **Not recorded** until a change ledger exists. A Prepare
Task Brief can say **None declared**, with that qualification.

**Settings → Cycle inactivity threshold** controls when activity becomes
**No output for 02:14 · timeout in 17:00**. Silence alone does not mark a step
failed. Plugins can publish diagnostic progress with
`context.progress(step.id, title=..., current=12, total=74, unit="modules")`.
Omit current/total when unknown. Agent stages and output update activity;
`scenario.run` tails complete child events for live scenario/step progress.
Progress is never a success receipt.

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
    reports/             node snapshots, for example run.json and run.html
      final/run.json, run.html                 latest terminal result
      final/<resume>-<phase-retry>/run.json, run.html
    .execution/reports/  final report receipts with artifact hashes
```

Artifact references are relative to this directory, so copying it preserves
their links. Task Brief and plugin outputs may also record the actual local
workspace/source paths and task content; a run archive is the execution record,
not an anonymized export. Use the baseline audit for sanitized summaries.

Report nodes capture the state when they execute, with `report_kind: snapshot`
and a capture time. Their `path` output refers to that immutable node artifact;
`final_path` points to the latest terminal result, including after Resume. After the run
ends, a run that executed a report node publishes a separate final JSON/HTML
pair with `report_kind: final`, the actual outcome, duration and settled steps.
Each Resume/phase retry has its own immutable final directory and receipt;
current reports link to this evidence, and earlier snapshots and their hashes
are never rewritten. Headless safe Pause is not a terminal result.
Final export failures are logged and recorded as diagnostics without changing
the already saved execution verdict.

`index.json` beside them lists the runs, newest first, each with its subject
and the step it got to - which is what the sessions are grouped from.

---

## Tags, and what is not here yet

Deliberately absent for now, so nobody looks for them: event sources other
than a watched step (see *Starting when a new task appears*), analytics
plugins, nested cycles, loops in the graph, and a plan that changes itself
during a run - see [the adaptive cycle design](cycle-adaptive-design.md) for
where that is going.

A wait lives **inside one run**: the process stays up for it. Pause can export
a safe checkpoint for later explicit Resume; it does not keep a background
daemon running after the application closes.

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
`peek(settings, each=False)`: what the step would take now, as a list of
`{"key", "title"}`, without running anything or writing anywhere - its own
limit and all. `each=True` is asked when the cycle runs on one item picked by
key: then every item that could be picked, not only as many as the step takes. That is all a cycle's `triggers:`
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
