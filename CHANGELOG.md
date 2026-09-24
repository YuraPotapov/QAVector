# Changelog

All notable changes to this project are documented here, in
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) form. Versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

**What the version is a promise about** — see *Compatibility* in the README. In short,
the public API is: the CLI flags, the `users.json` schema, the YAML flow format, the
report artifacts, and the on-disk profile layout `<env>-<login>`. That last one matters
more than it looks: changing it orphans real logged-in sessions on every user's machine,
so it is a MAJOR change even though no code signature moved.

While at `0.x`, breaking changes may land in a MINOR bump. `1.0.0` will be tagged once
the surface above is settled — deliberately *after* the planned work to make the engine
app-agnostic, since that will break things on purpose.

## [Unreleased]

## [0.16.4] - 2026-09-24

### Fixed
- **Running one step refused because a later step had changed.** Run step
  borrows every other step's last result so the graph stays filled in, and a
  step edited since - even one far after the selection that it never reads -
  made the whole run refuse with "Saved settings for ... changed". Only a
  changed step the selection depends on refuses now; a later one is left out.
- **Running one step mixed its new answer with the last run's.** Results after
  the selected step were lent from the last run, though they were made from
  what the step said then; a step that runs every time and has an `if:` then
  started at once on those old answers - before the selected step had run -
  and failed reading its outputs. Nothing after the selection is lent now,
  and such a step is left out of the run rather than started early.

## [0.16.3] - 2026-09-24

### Added
- **`min_effort` on every agent step** - a floor under Effort. With Effort
  taken from a review's judgement, a task judged easy is still reviewed at no
  less than the floor; the step says when it raised the level ("at medium
  effort (raised from low)").

### Fixed
- **The development cycles' code review never saw the change.** It was told
  to read `git diff` and has no shell, so it compared files against what the
  plan said they would be. A new `diff` step captures the change - files git
  does not track yet included, nothing staged - and the review and the
  acceptance are given it.
- **A commit's body began with the settle step's remark about itself**
  ("Unchanged. DEMO-2 is clear enough..."). The settled plan is now only the
  plan, written to stand as the body of the commit that carries it out.

## [0.16.2] - 2026-09-24

### Added
- **Agent steps say the effort they ran at.** `agent.review`, `agent.edit` and
  `agent.implement` end the first line of their message with the level -
  `at low effort`, or `at the CLI's default effort` - and publish it as an
  `effort` output. The CLI never reports it back, and a level taken from a
  review's judgement was otherwise visible nowhere in the run.
- **Settings -> Cycle runs** chooses where cycle runs are kept, and
  `--cycle-runs-dir` does the same from a terminal. A run, its resume and the
  Subjects list are all pointed at the one folder, so a list never reads a
  different place from the one runs were written to.
- **A plan says what it leaves out, and the person approving it sees that.**
  `scope: true` on an `agent.review` step asks it to list everything the task
  leaves unsaid that the work deliberately does not handle, published as
  `out_of_scope` with an `out_of_scope_count`. The development cycles' plans
  use it, their business review may object to something left out that the
  task requires, and the approval window shows the list under *Left out of
  scope* - so where the line was drawn is a person's decision, and *Send for
  revision* is how to move it.
- **The development cycles try to break the change before committing it.** A
  new `probe` step has a second agent write tests aimed at the change - odd
  inputs, boundaries, state after an error, rules the task implies - in the
  run's own folder, and `probe_run` runs them against the checkout
  (`probe_runner`, pytest by default). The code review reads the result and
  sorts each failure into a defect in the task (blocking), a real defect
  outside it (low) or a probe asking for something nobody wanted. Nothing the
  probes write is committed.
- `agent.edit` takes `read_only` - a directory the agent may read but not
  change, where a write fails the step - and `create_directory`.

### Changed
- **Reviews say what is wrong beyond the task, without blocking on it.** The
  development cycles' code review and acceptance now report defects outside
  the task's criteria as low severity: an unguarded input, a case that breaks,
  business logic that looks wrong. Only high and medium stop a commit, as
  before; low is there for a person to read rather than hidden.

### Fixed
- **A development cycle's commit had a paragraph for a subject.** The bundled
  development cycles put the plan's whole summary on the subject line, opening
  with whatever the plan said about itself. The subject is now the task's key
  and title; the plan is the body.

## [0.16.1] - 2026-09-24

### Added
- **A review can judge how hard the task is.** `assess: complexity` on an
  `agent.review` step adds a `complexity` - `low`, `medium`, `high`, `xhigh` or
  `max`, the effort scale - to what the review answers, and publishes it as an
  output, so later agent steps can take `effort:
  ${steps.<id>.outputs.complexity}` instead of a level fixed when the cycle was
  written. A review asked to judge that answers without a level fails its step.

## [0.16.0] - 2026-09-24

### Added
- **Cycles say what they work on.** A cycle may declare `subject:` - a kind, a
  `${...}` key such as the Jira task a step took, a title, the memory record
  that holds its state, and a `pin` variable that fixes it. Runs record it and
  announce it (`cycle.subject`), and runs of one cycle on one subject form a
  session.
- **A Subjects list on the Cycles page**, to the right of the canvas: one row
  per session with where it got to, what the cycle remembers about it and every
  run on it. Click a row to put its latest run back on the canvas, where Resume,
  Run step and Run from here act on it; the bin deletes the session's runs and
  memory record (never a branch or an issue). `--cycle-sessions` and
  `--cycle-session-delete` do the same from a terminal.
- **Every run says how it began** (`cycle.run.mode`): fresh, resumed or
  partial, what it kept and what it does again and why. Plan revisions are
  announced as `cycle.revision`.
- `jira.issues` takes `issue` - exactly one issue, bypassing the queue - and
  returns the first issue's `title` beside its `key`.
- **A panel dragged shut on the Cycles page leaves a bold line** where it went;
  clicking the line opens it again.

- Approval gates can return human feedback to a configured planning step with
  **Send for revision**. The bounded review path runs again and asks for approval
  on the revised plan. Feedback, prior plans and operation receipts persist;
  journaled checkpoint resets recover without restarting the whole cycle.
- Cycle **Resume** continues an exact saved execution with durable per-node
  receipts, definition/input fingerprints, artifact checks, an exclusive run
  lock and continuation history. Completed paid nodes remain completed when a
  later file writer fails. Agent adapters retain service responses before
  writing reports; an uncertain provider outcome stops instead of silently
  paying again. The main **Run** button becomes **Resume** after a stop or
  failure; **Hard Run** in its dropdown explicitly starts fresh. The CLI accepts
  `--cycle-resume=RUN_ID`; older runs can still use explicit partial execution.
- The agent steps say how hard the model should think. `agent.review`,
  `agent.edit` and `agent.implement` take `effort` — `low`, `medium`, `high`,
  `xhigh` or `max` — and pass it to the Claude Code CLI as `--effort`. Left
  blank, which is the default and what every existing cycle does, the CLI uses
  whatever it is configured to use. A level the CLI does not know is refused
  before the step starts rather than left to the binary, which warns and
  quietly runs at its default; the level belongs to the `claude_cli` backend,
  and CrewAI or AutoGen say so instead of ignoring it. In `agent.implement` it
  covers every attempt and the review that signs them off, so the reviewer is
  never asked to think less than the writer did.
- **The last cycle run is still there after a restart.** Everything a cycle
  reported lived only in memory, so closing the window lost the graph's
  colours, the output and the stages of a run that may have taken an hour. The
  core now writes every event it sends into the run's own directory as
  `logs/cycle.jsonl` — the file `cycle/workspace.py` has always documented —
  and the application replays it on the way up. A run the application was
  killed during comes back marked interrupted rather than claiming to be going.
- `jira.issues` downloads the pictures on the issues it read and says where
  they landed. Each issue carries an `attachments` list, and the step publishes
  `attachments`, `images` (the downloaded pictures' paths, to hand to a step
  that can look at them) and `attachment_count`. `attachments: images | all |
  none` chooses what is fetched and `attachment_bytes` caps the step's whole
  download. A file that was not fetched is still listed with the URL it is at,
  and one that could not be fetched costs that file rather than the issue. An
  image in a description now reads as `[image: name]` instead of vanishing.
- `jira.html` shows the pictures themselves, where the description put them,
  captioned with the file each came from. Every attachment the body did not
  show — one nothing refers to, one that is not a picture, one past the
  budget — is listed under the description. Pictures are embedded as `data:`
  URIs, so the page is still one file that loads nothing remote.
- A plugin can say its result belongs to the run that produced it
  (`PluginMetadata.reusable`, published by `--describe`). `approval.gate` is
  the case it exists for.
- Artifacts renders HTML reports directly with their CSS, with a Preview/Source
  selector. Links to neighboring artifacts select them in the tree; web links
  open in the browser. Report scripts and remote resources are disabled.
- Jira issue steps also write `jira.html` beside the unchanged `jira.json`:
  a standalone reading copy with formatted descriptions and comments, using
  the original Jira document structure without additional API requests.
- Development cycle (In Progress) prepares the exact Jira task branch from a
  freshly fetched base branch, reusing local or remote task branches and merging
  the current base before planning. Git commit refuses to stage or commit on a
  branch that differs from the task key.
- `git.prepare_branch` takes `offline`: with it set, a remote that cannot be
  reached no longer stops the step. It says so in the step's output, prepares
  the branch from the last fetched base or the local base branch, and reports
  which one through the new `offline` and `base_ref` outputs. The In Progress
  cycle sets it; everything else that stops the step still does.
- `memory.recall` takes `default`: what `value` is on a key nothing has written
  to yet. Without it a counter read before its first write compares as "not
  numbers", which is what a budget gate needs in order to read as unspent.
- A gate's row in Stages says what it decided: a verdict in words, then every
  check on its own line with a pass or fail mark beside it and the values that
  were actually compared. Rows are headed by what a step is called rather than
  by its id. Numbers are shown as numbers and an empty value is named.
- The canvas remembers where a cycle was being looked at from. Zoom and pan are
  written down about a second after the last change, per cycle, beside the node
  positions in `cyclelayout.json`, and restored when the cycle is opened. Fit
  and Arrange forget them again.

- **An agent step no longer needs an API key.** Reviewing code with an agent used
  to mean opening a web console, making a key, and working out where to export it
  so the right process would see it - a developer's errand standing between
  opening the application and having an agent read a failing test. `agent.review`
  now defaults to a backend that runs the Claude Code CLI, which signs in through
  a browser (`claude auth login`) and keeps the credentials to itself. No token
  passes through this application: nothing reads one, stores one or writes one
  down, so a cycle file that uses it carries no secret and is safe to commit. The
  CrewAI and AutoGen backends are unchanged and still take a key.

- **A plugin can say how it is set up, and the Inspector offers it.** Selecting a
  step shows a **Setup** section with whatever that plugin declares it can be
  asked outside a run - for `agent.review`, whether it is signed in and a button
  that signs in. The page has no idea what any of it means: a plugin declares
  `metadata.actions`, `--describe` publishes them, and the Inspector renders what
  it finds. Setting a plugin up therefore belongs to the plugin rather than to a
  row in Settings, which is what lets a plugin added later bring its own setup
  with it. `--cycle-plugin-action=PLUGIN:ACTION` is the same thing from a
  terminal.

- `git.checkout` cycle plugin: clone a repository into the run workspace, select
  a branch/tag or commit, optionally initialize submodules, and publish the
  checkout path and commit with per-command logs.
- `agent.review` cycle plugin: optional CrewAI and Microsoft AutoGen workers
  with configurable models, repository reading tools, structured findings and
  a JSON review artifact. Framework dependencies use a separate Python
  environment; cancellation and timeouts use the existing cycle controls.
- A Git-to-agent review example and framework setup guide in `docs/cycle-agents.md`.

- **Cycles: a new section that runs services, scenarios, commands and reports as
  one graph.** Until now every one of those was started by hand from its own page,
  and "bring up Postgres, bring up Odoo, wait for it, run the smoke suite, collect
  a report" was a thing you did in order, watching. A cycle is that written down
  once — as a graph rather than a list, because the interesting part is which steps
  wait for which. **Steps that do not depend on each other run at the same time**,
  so two suites against one server take as long as the slower of them rather than
  both. Write one in `cycles/`, open it on the new Cycles page, and press Run; see
  `docs/cycles.md`.

- **`jira.transition`: move an issue along its workflow.** `jira.issues` reads
  and says so in its first line, which is what lets a cycle point it at a
  production instance without thinking; this writes, so it is a separate plugin
  and the cycle file says which. The transition is **named, never guessed** — a
  workflow is the project's own, and a name that is not on offer is refused with
  the names that were, rather than becoming a 400. `lands_in` says where the
  move should end up, which is what makes a re-run safe: a workflow stops
  offering a move once made, so a run killed between the transition and the
  record would otherwise come back to an error — told the destination, the step
  recognises an issue already there and reports `changed: false`. It is checked
  afterwards too: landing somewhere else fails the step rather than reporting a
  success nobody verified. `expect_status` refuses when the issue moved under
  you. No create, no assign, no delete.

- **`jira.issues` also reports the first issue's key on its own.** `${...}`
  cannot index a list, so a step handed `keys` got the whole list where it
  wanted one issue.

- **`cycles/development.yaml`: one Jira task to a verified commit.** The whole
  chain, and the first cycle that uses every piece of the section at once: take
  a task, ask an agent whether the code already does it, plan it, have a
  *second* agent review that plan against the project's rules, ask a person,
  move the issue, make the change and prove it, review the result
  independently, accept it against the criteria, and commit the tree that
  passed. Twenty-three steps and four gates, each of which stops the run and
  says which condition it was. It asks once, between the plan and the first
  line of code — the cheapest moment a person can be asked, with the plan in
  front of them and nothing yet written — so it needs the application. One task
  per run; it remembers what it finished and declines it next time without
  paying an agent to work that out. `cycles/task_to_commit.yaml` is unchanged
  and is still the short unattended version.

- **`check.gate`: several things that must all hold.** A condition on a step is
  one comparison on purpose — `if:` has no `and` and no `<` — and that is right
  for an edge in the graph and wrong for the place a run decides whether it has
  earned the next step. Written as conditions, a four-part gate was a chain of
  four empty steps, four nodes of noise on the canvas, and a refusal that said
  "if: … was not true" without saying which. This is one node, and its message
  names the check that refused and shows the values: *the review is clean:
  '1' == '0'*. Equality is imported from `if:` rather than rewritten, so the
  two cannot drift apart.

- **`agent.review` counts its findings.** `${...}` walks mappings and cannot
  measure a list, so "did the review find anything" was unaskable from a cycle
  file. `issue_count` and `blocking_count` are the two numbers a gate wants,
  the second being the useful one: a low-severity note should not stop a run.

- **`jira.issues` takes the ordering.** It was `ORDER BY updated DESC`, written
  into the code, and an ORDER BY in the extra `jql:` was a syntax error because
  that clause is bracketed. It decides which issue a step with `limit: 1` gets,
  so a cycle that takes one task per run was having its queue chosen for it.

- **A cycle stays arranged the way you left it.** Dragging a node out of the
  way lasted exactly as long as the window did: reopen the cycle, or restart
  the application, and the arranging was gone — so nobody bothered arranging.
  Positions now live in `cyclelayout.json` beside the GUI's other files.
  Deliberately **not** in the cycle file, which is committed and shipped and
  where a box sits on somebody's screen is none of its business. A step the
  file does not mention goes where the layout computes it, so an arrangement
  survives the cycle growing a step, and **Arrange forgets** rather than only
  undoing — otherwise the mess would come back on the next open.

- **The canvas runs down the page.** It ran left to right, which is fine for
  the six-node demos and unreadable for anything real: a twenty-step cycle is
  an ordinary shape and twenty nodes across is five thousand pixels of line.
  Down the page it is a list, which is a thing people read all day, and a fork
  spreads sideways and becomes the widest thing on the page — which is what you
  want to notice. The direction is a value in the canvas's spec, so it is one
  arithmetic rather than two that can disagree. A long cycle also **opens at a
  size its labels can be read at** and scrolled from the top, rather than
  fitted whole into the window at a scale that answers "how big is it" and no
  other question; Fit still shows all of it.

- **Every dialog is the application's own now.** `QMessageBox` is not styled
  by the stylesheet - it draws its own icon, asks the platform for its buttons
  and keeps the desktop's frame - so all 64 of them, across eleven files,
  arrived looking like every other application on the machine and none of this
  one. They now go through one `widgets.Message`: the title bar the main window
  wears, the theme for nothing, dark mode included. Three shapes - a question,
  a notice, and a choice of three, because "continue this one / start a new one
  / cancel" does not fold into a yes/no - plus a mono box for the dialogs that
  carry what a command printed, where somebody wants to select a line rather
  than read a label. Buttons are named after what they do: **Overwrite**,
  **Discard**, **Delete**, **Stop all and close**. Ten other windows - the
  service console, the row editors, Settings, the step editor, the log viewer -
  wore the desktop's frame too, and now wear ours. Taking that frame off takes
  its resize handles with it, so they are put back: a window that could be
  resized still can, and only a dialog that never had a frame - a confirmation
  - goes without. The windows somebody *works* in rather than fills in keep
  minimize and maximize as well.

  Two things fell out of doing it. A test helper answered confirmations with
  `QMessageBox.No`, which is a **non-zero** enum member: the day the pages
  started returning a bool, every refusal in those tests quietly became a
  yes. And the frame held a reference to its window while the window held one
  to its frame - a cycle between two QObjects, which Python collects in an
  order nobody decides. One frame on a window that lives for the whole process
  never showed it; a frame on every dialog **segfaulted**, on the next dialog
  rather than the one that went.

- **A cycle run says where its files are.** It never did, and only the
  scenario engine sent that event - so a cycle's reports, its agents'
  transcripts and the JSON a Jira step saved were written to disk and then
  unreachable: History had no directory to remember and the Artifacts page had
  nothing to open. One event, sent before the first step, and both pages work
  with no change of their own.

- **The output panel shows what a step came to, not only what it printed.**
  Half the plugins print nothing at all - a Jira step returns issues, a memory
  step returns what it remembered, and neither says a word on the way past - so
  a run could finish, having done exactly what was asked, and leave the one
  place somebody looks blank. A finished step now closes with its status, its
  message, its outputs and the files it wrote. It is appended rather than
  substituted, so a step with real output keeps it.

- **Settings -> Cycles.** There was a path setting for the secrets store, for
  the projects file and for the memory store, and none for the cycles
  themselves - the GUI never passed `--cycles-dir` at all. In a source checkout
  the core's default is the checkout, so the cycles somebody edits are the
  template files that ship with the application, and saving one writes their
  own Jira instance and work email into a file that is committed. That had
  already happened. A test now refuses to let a shipped template name a real
  host, a real address, or a path from the machine it was edited on.

- **Running part of a cycle.** Working on one step of a twenty-three step cycle
  meant running all twenty-three, which on the development cycle means paying
  for six agent calls to reach the seventh. Two chevrons beside Run act on the
  selected step: `>` runs that one, `>>` runs it and everything that waits on
  it. The steps left out are **taken from the last run** - their outputs go
  into scope, which is the only reason a step that reads another's result can
  run alone at all - and a partial run with nothing to take from is refused
  before it starts rather than failing several minutes in on a reference that
  does not exist. `--cycle-only` and `--cycle-from` from a terminal.

- **Starting a cycle asks first, and says what it is agreeing to.** A full run,
  and a run from a step onwards, both confirm - they spend agent calls and move
  a Jira issue, and Stop ends a run without undoing what has already happened.
  Running a single step does not ask: it is the cheap one, and the buttons
  exist to be pressed repeatedly.

- **Selecting a step lights up what it is connected to.** Its lines are drawn
  heavier and in the accent colour, and lifted above the lines they cross -
  still under every node, because a line over a node's writing is a line in
  the way. On a long cycle that answers a real question: a step's links run off
  in both directions past a dozen other boxes, and following one by eye meant
  tracing a grey line through every other grey line. Both directions are lit,
  not only the ones leaving it — "what does this wait for" and "what waits for
  this" are one question when you are reading a graph. It is a repaint and
  nothing else: a line that jumped when you clicked the node it belongs to
  would be worse than no highlight at all.

- **A node is tinted by what kind of step it is.** Agents one colour, git
  another, Jira another — keyed on the family of the plugin, which is the part
  of its id before the dot, so a plugin added later is grouped correctly the
  moment it is named. Reading a twenty-step cycle, the question "where does
  this one spend money" or "where does it touch the repository" is now a
  glance rather than a read.

  **Deliberately none of red, green or amber.** Those three already say what
  *happened* to a step, on the stripe down its left edge, and a body speaking
  the same language would give the canvas two meanings for one colour — a node
  tinted green for "this is the commit" would read as one that had already
  succeeded, before the run had started. So the stripe keeps the saturated
  colours and the body gets a wash: enough hue to group a graph, far too little
  to be mistaken for a verdict. Colour is not the only channel either, and
  while checking that, the plugin's id turned out to have been sitting at a
  contrast of 3.9 against the node — under the 4.5 small text wants, before any
  of this. It is a step darker now, and clears it on every tint in both themes.

- **Lines no longer pile up on each other.** Two edges drawn on the same pixels
  are one edge as far as a reader is concerned, and a column layout produced
  that by construction: every node shares an x, so an edge that skips a layer
  ran straight down *through* the nodes between its ends and along every short
  link on the way. On the development cycle that was 13 lines passing through
  nodes and 25 sharing a corridor — now none of either. Edges that would cross
  something go round it, in corridors beside the graph, longest outermost and
  alternating sides the way a transit map does. Edges that share a side of a
  node attach at **different points** along it rather than all at its middle,
  so three links out of one step read as three. Deciding this is the canvas's
  job, not the edge's: a line is only in the way of a node it does not belong
  to, and an edge cannot see those. It is recomputed as a node is dragged,
  because moving one step changes what every other line has to avoid.

- **A step can say "not yet" instead of finishing.** `needs:` said a step goes
  after another one; there was no way to say it goes after a *time*, and the
  nearest thing — a `sleep` inside a shell step — holds a worker for its whole
  length, so a cycle with four of them and the default four jobs stops running
  anything at all. A step may now return `waiting`, which keeps its place in the
  graph, **gives its worker back**, and is come back to when the time is up.
  Everything that needs it waits too, which is what `needs` already meant, said
  about time. `time.wait` is the plugin that does it — `seconds:`, or `until:`
  a clock time or a date — but the mechanism is any plugin's to use. It is
  deliberately not a retry: a retry is what happens after a failure, and a
  reader who cannot tell the two apart sees a healthy cycle as one failing over
  and over, so it has an event and a colour of its own. A step's `timeout:` now
  bounds its **whole life**, waits included, rather than each turn of it — both
  the honest reading of a deadline written on a step and the only thing
  stopping a plugin that would ask to wait for ever.

- **`approval.gate`: a cycle can stop and ask you.** Everything else a cycle
  does it decides for itself, which is right up to the point where it is about
  to edit a repository other people work in or move something on a board other
  people read. A gate puts the question and whatever the decision rests on — a
  plan, a diff, the findings — in a window, and the run waits. **Not answered is
  not approved:** no application attached, nobody at the screen, the window
  closed, the deadline passed — each of those fails the step, and there is
  deliberately no setting to change it, because a gate that approves when it
  cannot ask stops being a gate exactly where somebody believed they had one.
  That is also its cost: a cycle meant to run unattended should not have one,
  and should delete the step rather than disable it. The step's own `timeout:`
  is the deadline and the window counts it down. `cycles/review_and_fix.yaml`
  now asks before it lets the second agent spend money.

- **Cycles remember things between runs.** A run used to start knowing nothing:
  whether this task was already done, how much of a budget it had spent, whether
  another run was on it right now — none of it was askable, because the `${...}`
  scope is entirely about the run in progress. Three steps now sit over a small
  keyed store: `memory.recall` reads, `memory.remember` writes, and
  `memory.claim` takes a task for the run and gives it back when the run ends —
  held by the run, so a crash, a timeout and a Ctrl+C all release it. A second
  run of a claimed task fails saying who has it and that this is a failure of
  timing rather than of the work. Counters move under the store's own lock,
  which is what makes a budget that survives a restart bound anything, and the
  store is plain JSON on purpose: "why did it skip that task" is a question
  somebody will ask. Path in Settings → Cycle memory; readable from a terminal
  with `--cycle-memory-list` / `-show` / `-forget`.

- **`agent.implement`: make a change and prove it.** `agent.edit` makes one pass
  and stops, which is right when a person is going to read the diff and wrong
  when the step is meant to *finish* something — an agent that cannot run the
  tests cannot know whether what it wrote works. This one edits, runs the checks
  you name, and when they fail goes round again with the failure in front of it,
  up to a budget it owns. `verified` is never the agent's own word for it: it is
  true only when every check exited zero and a second agent, given the reading
  tools and nothing else, signed the result off. Only high and medium findings
  send the work back — spending the budget polishing nits is the opposite of what
  the budget is for. The loop lives **inside the step** on purpose: what a person
  reads off the canvas is "make the change and prove it", and how many times the
  agent went round is the step's own business, the way `retry` is.

- **`git.commit`: commit the tree that was verified, and refuse anything else.**
  Committing is one command and `command.shell` can do it. What a shell step
  cannot do is answer the two questions that make an automated commit
  trustworthy. *Is this what passed?* — `agent.implement` records the git tree
  hash at the moment the checks agreed, and this refuses if the files changed
  since, so "verified" and "committed" are the same object rather than two
  descriptions that usually agree. The hash is taken against a temporary index,
  so a refusal leaves your own staging untouched. *Did I already do this?* — the
  intent goes into the message as a `QAVector-Operation:` trailer and a re-run
  finds its own commit instead of making a second, which is what a run killed
  between the commit and the record needs. Local only: no push, no branch, no
  merge. `cycles/task_to_commit.yaml` is the whole chain in one file.

- **`jira.issues`: what is on somebody's plate, and what has been said about it.**
  One step — a search plus the comments on what it finds — rather than a Jira
  client: no create, no transition, no edit, so it can be pointed at a production
  instance without thinking about it. Reads by `assignee`, `reporter`, `creator`
  or all three, across every project the account can see or only the ones named
  in `project`, takes extra JQL, and hands back a list a later step can walk or an
  agent can be given. No new dependency: `urllib` from the standard library, the
  way the engine already reaches an HTTP server. The API token belongs in the
  secrets store and is referenced as `${vars.…}` — cycle files are committed and
  shipped. Cloud and Server are named by a field rather than guessed at: Atlassian
  removed the old search endpoint from Cloud and sends comment bodies as a
  document tree, which this flattens to text. Display names only — an email
  address arrives in every issue and nothing downstream needs it.
  `cycles/todo_to_plan.yaml` is the whole idea in one file — what is in To Do,
  with its comments, turned into an ordered plan by an agent.

- **`agent.edit`: an agent that changes files, as a step of its own.** `agent.review`
  reads and only reads — that is what lets you point one at a real checkout without
  thinking about it. When you want the change made rather than described, this is
  the step: it gets `Edit`, `Write` and `MultiEdit` on top of the reading tools and
  runs with `--permission-mode acceptEdits`, because nobody is at a terminal during
  a run and a step that waited to be asked would hang. It is deliberately **not** a
  setting on the review — a switch would mean every existing review step changed
  meaning depending on one line further down, while the file read the same. It is
  also deliberately given **no shell**: a step that needs to run something has
  `command.shell`, which says so in the file. What it changed is read off the CLI's
  own event stream as the writes happen, so `files_changed` is exact and survives a
  step that failed halfway. `cycles/review_and_fix.yaml` is the whole arrangement
  in one file — review, fix, and a check that the fix did not break what already
  worked. See `docs/cycle-agents.md`.

- **An agent step shows its work as it happens, not one wall of JSON at the end.**
  A review used to print the blob it finished with into the output — a single
  unreadable line that arrived only once the work was over and said nothing about
  it. The `claude_cli` backend now runs the CLI with `--output-format stream-json`
  and turns each turn into a row in a new **Stages** tab: what it is weighing,
  which file it opened, what came back, what it concluded, what it cost. The raw
  stream stays out of the output and is still written whole to the step's
  `stdout.log`. Stages are a general mechanism (`cycle.step.stage`) — any plugin
  that emits them gets the same rows with nothing added to the interface.

- **The output panel shows the whole run until you ask for less.** A cycle runs
  several steps at once, and the panel used to show nothing at all until you
  clicked a node and guessed right about which one was talking. With nothing
  selected it now shows every step's output, each line saying which step it came
  from; selecting a node narrows both the output and the stages to that step, and
  the tab says whose they are.

- **Variables can be secret, and a secret's value never enters the cycle file.**
  A cycle file is committed and ships inside the build, so a value written there
  travels to everyone who clones the project. A variable declared
  `{secret: true}` puts only its name in the file; the value is encrypted beside
  your own data, with the key at mode 0600 and the path in Settings → Cycle
  secrets. The properties dialog edits variables as a table — name, value, type —
  where a secret's box is masked, a stored one opens empty rather than showing
  what it holds, and dropping a secret takes its value out of the store.

- **The Cycles page draws the whole cycle at once, and the same drawing is the run
  view.** Opening one shows every step and every connection between them on a
  canvas; selecting a node shows what it runs, what it waits for, and — once it has
  run — what it came to. Starting it does not move you to another page: the nodes
  already on screen take on their status as it goes. There is deliberately no second
  picture of a run to keep in step with the first.

- **Eight things a step can be, and a way to add more.** `command.shell`,
  `service.start` / `stop` / `restart` / `wait`, `scenario.run`, `report.json` and
  `report.html`. The service ones ask the application to act, through the same
  request-and-reply the Services page already answers for scenarios; `scenario.run`
  runs the launcher, which is how the GUI has always run scenarios, so nothing about
  profiles, logins or reports is reimplemented. Each one declares what it takes and
  what it produces, `--describe` publishes that, and the page builds its Inspector
  from it — so a plugin added to the core appears in the interface without the
  interface changing.

- **Steps read each other's results rather than knowing about each other.**
  `${steps.postgres.outputs.port}` is how a step gets a port from the step that
  started the service, and a reference that is the whole value keeps its type, so a
  port stays a number. A reference to something that is not there is an error that
  names what *is* there, rather than an empty string in the middle of a command.

- **`--cycle-run`, and four commands beside it.** `--cycle-list`, `--cycle-show`,
  `--cycle-save`, `--cycle-delete` and `--cycle-import` answer with JSON the way the
  `--flow-*` commands do, and `--cycle-run=ID --events=-` runs one from a terminal
  with no GUI at all. Service steps in a headless run fail in the first millisecond
  saying they need the application, instead of waiting out two minutes each for an
  answer that is not coming.

- **Every run leaves one directory.** `~/QAVector/cycle-runs/<when>-<cycle>/` holds
  the record, the graph as it was, the event stream, each step's output, and any
  report. It is written *as the run goes*, so a machine that dies halfway leaves a
  readable partial run rather than nothing. Nothing in it is an absolute path, so
  the directory can be zipped and read on another machine — which is the whole point
  of putting it in one place.

### Changed
- **`agent.implement` runs without checks.** An empty `checks` makes the review
  the only verdict; the result says "by the review only; no checks were run"
  and a new `checked` output is false. With no checks *and* no review it still
  refuses, since nothing would verify the edit.
- **Agent steps no longer have a turn limit unless they set one.** `--max-turns`
  is passed to Claude Code only when a step sets `max_iterations`; the step's
  `timeout` bounds it otherwise. A default of 30 stopped an implementation
  halfway through a six-file change.
- **A step that runs past its timeout asks before it is stopped.** In the
  application, a window offers Continue (another timeout) or Cancel while the
  step keeps working; no answer stops it. Runs from the command line, approval
  gates, waits and service steps keep the old behaviour.
- **`agent.implement` continues earlier work instead of starting over.** When
  the checkout is exactly what an earlier run of the step left, it is checked
  and reviewed first; if it holds, the step is verified without another paid
  attempt, and if not, the next attempt carries on from it. Work that a later
  step in that run (a review or acceptance) found blocking issues in goes back
  for rework with those findings, even when the checks pass.
- **`agent.implement` runs its checks once before the first attempt.** Checks
  that fail on the untouched checkout stop the step at once, and a failure an
  attempt leaves unchanged stops the loop rather than spending the budget.

- **Two buttons about the command line are gone.** "Copy command" in the toolbar
  and "Open in Command" on a History entry both existed to get somebody to the
  launcher's argv. A cycle or a scenario run restored into the Command page is
  not a thing anybody wanted to look at, and Run again is what that button was
  really being used for. A *launch* can still be opened in Launch Sessions,
  which is the half that was useful; the Command page keeps its own Copy.

- **Two runs of the same thing in the same second no longer share a directory.**
  Cycle runs are named to the second like scenario reports are, but a second run
  inside that second gets a suffix instead of writing into the first one's files.
  (Scenario reports still behave as they did; this is the new code not inheriting
  the flaw.)

### Fixed
- Resume refused every run descended from one that predates step fingerprints
  ("Original settings were not recorded for imported steps"). Such steps are
  now checked against the run's own manifest.
- Approval windows now use the application frame and a larger resizable review
  area. Plans support headings, lists and readable findings; risk and review
  counts stay above the document, with decision buttons and the deadline below.
  An Original text tab preserves the exact supplied detail.
- **Output for implementation steps is now a readable report.** It explains
  the outcome, reported file changes, verification, duration and cost, with a
  next action for an agent step limit. Existing saved errors are normalized
  too. **Technical details** retains the original log and outputs; artifacts
  keep their original format.
- **A plan the review objected to now gets a second pass instead of ending the
  run.** In the Development cycle (In Progress) one medium finding used to fail
  the `plan_is_sound` gate, which skipped the approval, the work and the commit:
  the plan never answered the objection and the attempt was spent for nothing.
  The gate is gone. `settle` takes the plan, the objections and the rules and
  closes what a plan can close, leaving open — and named — anything needing a
  decision that is not its to make; `recheck` is a fresh reviewer reading only
  the settled plan; and `approve` then always asks a person, showing both
  reviews' counts and what is still open. `preflight` asks only that a person
  agreed, that the task is still claimed, and that it is the same task.
  Everything downstream builds, reviews and commits the settled plan.

- Partial runs accept skipped optional report dependencies, preserve saved
  failure statuses and copy reused artifacts into the new workspace.
- A conditional cycle step that reads an output from a skipped step now also
  skips, explaining which prerequisite produced no outputs. This prevents
  `Remember it was already done` from failing after an earlier gate or agent
  stopped the development cycle, without mistaking a missing verdict for a
  completed task. Unknown steps and missing outputs from steps that ran still
  report errors.
- **An approval gate is never taken from an earlier run.** Running part of a
  cycle borrows the steps it is not running from the last run of the same
  cycle, and `approval.gate` was borrowed like any other — so a gate somebody
  answered an hour ago passed again in every run after it, and one that had
  been *skipped* was recorded as `success` with no outputs at all, which then
  chained: each run inherited the previous run's phantom approval. Nobody was
  asked, and the cycle went on to edit a repository on the strength of it. The
  gate is now asked in every run, and nothing decided on an old answer is
  inherited either — a step below the gate is decided again too.
- A partial run no longer borrows a step that never ran. Only a step that
  actually produced something can be lent; a skipped or cancelled one is
  refused up front with the message that already exists for it, instead of
  becoming a `success` whose outputs a later `${...}` resolves against nothing.
- **A cycle with many stages no longer hangs the window.** The stage column was
  rebuilt from nothing on every event of a run — a stage, but also every batch
  of a step's output — so drawing n stages cost the sum of 1..n widget trees,
  each with a stylesheet to parse. It appends now, and the page asks for one
  repaint per turn of the event loop rather than doing one per event. A run of
  300 stages builds 300 rows where it used to build 45,150.
- **Stages no longer empties out.** It showed only the selected step's stages,
  and only an agent step reports any, so clicking any other node blanked the
  one readable account of what the cycle was doing — and a stage from an
  unselected step did not even ask for a repaint. Stages now always lists the
  whole run with each row headed by its step; picking a node narrows the
  Output tab, which is what picking is for.
- Starting a scenario run no longer wipes the cycle a page is showing. Both
  runs shared one reset and one "is it running" flag, so a browser session
  started on the Run page left the Cycles page with a coloured graph beside an
  empty Output and Stages, and lit Stop for a cycle that had finished long ago.
- Jira issue steps read the files on an issue. The step never asked Jira for
  the attachment field, so nothing downstream knew a screenshot existed — and
  an image inside a description was dropped from the text without trace, which
  is worse: a task whose whole specification was a picture arrived as a
  description that did not mention one.
- `git.prepare_branch` works in a checkout with no remote. It refused one
  outright — "No configured remote named 'origin'" — before reaching any of
  the offline handling that exists for exactly this, though there is nothing
  unreachable about a repository that was never given a remote and nothing
  misconfigured either. A checkout with no remotes now prepares the branch
  from its local base whatever the settings say; the only thing that makes a
  directory unworkable is not being a git checkout. A remote that is *named*
  and does not exist is still reported — a typo should not quietly build on a
  stale base — and follows `offline` like a failed fetch, saying which remotes
  the checkout does have.
- Both development cycles spend an attempt only on a run the gate admitted. The
  counter moved before the gate judged it, so every refused run — an already
  committed task, nothing assigned, a dirty checkout — climbed a budget nothing
  could bring back down, until a task with an unrelated problem could not be
  worked on again without emptying the memory store by hand. The gate now reads
  the count as it was when the run started (`< max_runs_per_task`), and the
  counter moves right after it, still before any work.
- A cycle keeps its run on screen. Re-reading it - clicking it in the explorer,
  a Refresh, closing Settings, saving anything on another page - no longer
  rebuilds an unchanged graph, and where a rebuild is needed the nodes are
  repainted from the run the application is already following, instead of every
  step going blank while a run was still waiting.
- A cycle opens at the zoom the ruled paper appears at, with its first step at
  the top, rather than fitted to whatever shape the window happens to be. The
  two are now one number, so opening can no longer land on blank ground.
- The Settings form scrolls. Ten paths and their notes are taller than a laptop
  screen, and a dialog capped at the screen used to squeeze the notes into each
  other and cut off the last of them; Save and Test connection now sit outside
  the scrolling body where they cannot be pushed off the bottom.
- The full-cycle Run button no longer passes its boolean checked state as the
  text run mode, eliminating PySide6's "Cannot copy-convert (bool) to C++" warning.
- The first HTML preview no longer hides and recreates the main window. Its
  graphics composition is prepared before the window appears, while Chromium
  and the document still load only when an HTML artifact is selected.
- Opening an HTML artifact no longer crashes the main application in PySide6.
  Wheel guards now attach to dropdowns, spin boxes and their editors instead of
  filtering WebEngine's internal object events through an application filter.
- Cycle Properties preserve path variables and their values when saving and
  reopening. Paths were incorrectly written as unset secrets. Changing between
  path, text, and secret types now also updates the secret store correctly.

- **`and` in an `if:` was read as one long string and silently skipped the step.**
  A greedy match ran from the first quote to the last, so
  `${a.status} == 'ok' and ${b.status} == 'no'` parsed as a single literal:
  validation said nothing, the condition evaluated false, and the step it guarded
  was skipped with no indication why. Conditions still have no `and`, `or` or
  comparison — half an expression evaluator is worse than a small complete one —
  but the refusal is now audible, and it names the word you used.

- **The canvas is ruled paper now, not a field of dots.** Fine square ruling with
  a heavier line every fifth square — the surface the splash screen's artwork is
  drawn over, so starting the application and opening a cycle land on the same
  paper. Square and square on: the grid in that artwork runs at an angle because
  the whole scene there is drawn in projection, and copying the angle onto a
  canvas seen head on would be copying an artefact of the drawing rather than
  the thing it draws.

- **The canvas draws its connections as a diagram does.** Lines run along one
  axis and turn at right angles rather than sweeping through curves, and a fork's
  branches bend on the same line, so a split reads as one thing splitting rather
  than as several unrelated strands.

- **Dragging a step left its connections looping out and back.** Every edge left
  its node by the right side and arrived at the next by the left, which is right
  for the layout the core produces and wrong the moment somebody stacks two steps
  vertically — the first thing people do on a canvas they can drag. An edge now
  picks its sides from where the two nodes actually are, so stacked steps connect
  bottom to top and a step dragged to the left of the one it follows is reached
  without crossing it.

- **The output panel's tabs were Qt's own.** Shaded lozenges with a raised border
  — heavier than anything else on the page and reading as a different
  application. They are now the idiom the nav rail already uses: a strip of words
  with a rule under it and the live one marked by a bar in the accent.

- **The Stages tab opened at the top of a run instead of at the newest row.** The
  view scrolled to the bottom the moment its rows went in, which is one layout
  pass before the scroll range grows — so it landed on the old maximum, which for
  a fresh list is the top. Both the stages and the output now follow the newest
  line, and both stop following the moment you scroll up to read something and
  start again when you scroll back down.

- **A step whose plugin takes a mapping or a list could be opened and then not
  saved.** The cycle writer handed anything that was not a scalar to the same
  quoting rule a string gets, so `agent.review`'s structured inputs went to the
  file as `inputs: "{'expected': 'ok'}"` — a Python repr in quotes, which reads
  back as a string and fails validation on the next load. Saving a step with
  nothing changed was enough to trigger it. Nested mappings and lists are now
  written as YAML.

- **A value that YAML gives its own meaning to came back as something else.**
  `12:30` read back as the number 750 and `2026-09-19` as a date, because the
  writer quoted values by working through a hand-written list of risky shapes
  and that list is never finished. It now asks the parser whether the plain form
  reads back as the same string, and quotes when it does not.

- **A newline inside a mapping value was dropped on save.** The `name = value`
  box gives each pair a line, so a value containing a newline came back as a
  blank line and lost it — silently, on a save that changed nothing.

## [0.15.2] - 2026-09-11

### Fixed
- **The inputs in a scenario's steps sat off their row.** The action box on every
  step, and the field a cell turns into when you edit it, were drawn 3px low and
  hung over the line beneath: the rows were sized for text, and the table insets
  anything placed in a cell. The rows are now as tall as those inputs need, so both
  sit centred between their lines, as they already did in the Services and
  Environments tables.

## [0.15.1] - 2026-09-11

### Added
- **Launch Sessions → Choose scenarios has an *All / Selected* switch.** *Selected*
  narrows the list to what is ticked, in the same place, so a long selection reads at
  a glance without the page growing with it; *Clear all* unticks the lot. Both views
  carry their counts.

### Changed
- **History filters by what happened** — result (passed, failed, stopped),
  environment and period (today, the last 7 days) — instead of by the page a run came
  from. With nothing picked, the details say so on their own ground rather than as an
  empty heading beside a blank status pill, and the command line and its *Copy
  command* button are gone from them.
- **About describes QAVector as the application it is**: what it does, its version and
  its Qt, rather than a front-end to a launcher with a version and a Python of its own.
- **The *Developer mode* button is off the toolbar.** The mode itself is unchanged
  and stays under View → Developer mode (Ctrl+Shift+D). Settings shows the core
  script and interpreter only in developer mode; an installed build finds its own.

## [0.15.0] - 2026-09-11

### Added
- **Run in the background: RUN ▾ → *In Background*.** A scenario made only of service
  steps and `assert_host_up` — a backend restarted, waited for, checked — no longer
  needs a Chrome window to run in. This runs it with no browser at all: the Run page
  shows every session and step as usual, and sessions, how many run at once and
  *Auto*, with the load governor correcting it as the run goes, work exactly as they do
  with windows. The entry is greyed out, naming the scenario and its page steps,
  whenever anything selected needs a page, and the launcher refuses such a run too,
  before it starts. On the command line it is `--no-browser`; `--describe` now says
  per scenario which page steps it takes (`browser_actions`, `[]` for none).
- **Each scenario says how long it took.** The Run page puts the time beside every
  scenario in a session's panel, and a running one's time moves on while it runs,
  events or not — a backend test run can go minutes without one. The run's own
  clock reads *1 min 18 s* rather than *78.3 s*, and the run summary carries it
  too. Scenarios are timed by the launcher's clock on the events, not by when the
  window got round to drawing them.

### Changed
- **chrome-multi-session is now QAVector** — *Explore. Build. Verify.* The name on
  the window, the splash, the menus and the installers; the package and its
  commands (`qavector`, `qavector-gui`, under `/opt/qavector`); and the folders it
  keeps your things in. The new `.deb` replaces the old package. On first start the
  folders move to their new names — `~/ChromeMultiSession` to `~/QAVector`, the
  GUI's history under `~/.local/share` and its settings under `~/.config` — and a
  link is left at each old folder, so whatever still points there, a runner's
  script in `services.json` or a desktop link, keeps working. Where no link can be
  made the folder is not moved and the old one stays in use; nothing is ever
  overwritten. `$CMS_HOME`, `cms.ini` and the `cms_gui` module keep their names.

### Fixed
- **A scenario with a `name:` of its own showed twice on the Run page** — done,
  under its name, and again at the bottom by id, as though it had never run. The
  page filed a run under the name `flow.start` gave it and checked the planned list
  by id. `flow.start` now carries the scenario's `id` as well, and the page files
  by that.

## [0.14.5] - 2026-09-10

### Added
- **The GUI draws its own title bar.** The desktop's frame — GNOME's grey band
  reading "chrome-multi-session — GUI" in the system font — is replaced by one in
  the design: the mark, the name and the version on the icon's slate, with the
  window controls beside them. It moves, maximizes on a double click and resizes
  from every edge, all handed to the window manager so tiling and snapping keep
  working. Only the head changes; the menus and everything under them are as
  before. `CMS_SYSTEM_FRAME=1` keeps the desktop's frame.

## [0.14.4] - 2026-09-09

### Fixed
- **`wait_for_criterion` behind a `service_restart` answered from the run being
  killed.** A criterion is cleared by the service's own start, which for a service
  that is *already up* happens only once the old process is down — several seconds
  for a backend, and the step behind the restart arrives in milliseconds. So the tag
  still lit from the previous boot passed the wait instantly, and the scenario walked
  on into a server that was still shutting down; the same sequence against a service
  that had been *stopped* waited properly, because there the start had already run.
  One of the two had to be wrong and it was never the same one twice. A service owes
  the scenario a new run from the moment `service_start` or `service_restart` is
  accepted until it actually starts, and for as long as it does, neither its criteria
  nor its status can answer a wait. `wait_for_out` already kept this rule with a
  buffer of its own; the other two waits keep it now as well. A restart that ends in
  a failure ends the wait behind it immediately, rather than after two minutes of
  waiting for a start that has already not happened.
- **Opening a service's form and closing it lit up Save.** Nothing had been
  typed, and Save offered to rewrite the file anyway — not always, which made it
  look random: it happened on every service written before *Stop timeout* was
  added to the forms in 0.14.3, and on none of the few edited since. The form
  wrote back every field it draws, so a field the row had never carried came back
  as `"stop_grace": ""` — an empty value, a real change, and a page that says
  there is something to save. A field the row does not have and nobody filled in
  now stays absent; anything the row already carries is still written whatever it
  says, so clearing a field saves as before. On the file this report came from,
  85 of 92 services changed on an untouched open; now none do.
- **A service adopted from before the window opened showed no criteria at all.** A
  detached service that outlived the last session is picked up by its pid and its
  console starts at the live end of the log — which left every criterion grey for as
  long as the process lived, however plainly the log said *started*, because the line
  that lights one is written once at boot and never again. `wait_for_criterion` on
  such a service could only ever time out. The run's own output is now read back for
  the criteria when it is adopted, from the offset where that run began — remembered
  beside its pid, since the log is appended to across runs and its top is the boot
  before. A service started by an older build has no such offset recorded and is
  adopted as before, claiming nothing; restarting it once is enough.

## [0.14.3] - 2026-09-04

### Fixed
- **A restarted service died a few seconds later.** `stop()` arms a timer to kill
  whatever has not gone down by the grace period; a restart stops and starts again
  in well under that, so the timer came back to a running service — the *new* one —
  and killed it. From the outside it looked like whatever you did next had killed
  it, because that is what you were doing when the timer finally fired. The timer
  is now bound to the process it was armed for and leaves its successor alone.
- **Restarting a service that forks workers left the workers running**, and said it
  had worked. Only the process this application started was ever signalled, so a
  master/worker server — Odoo in prefork, gunicorn, anything with a pool — lost its
  master and kept its workers, which went on holding the port. The restart then
  started a new master whose workers could not bind, and the row said *running* over
  a backend that was not there. An attached service is now given its own process
  group (`CreateNewSession`) and the group is what gets signalled, so the workers go
  down with the master. Detached services were already in a group of their own and
  were signalled one process at a time; they take the same path now.

  The guard matters as much as the fix: a child that never got its own group is in
  *this application's*, and signalling that group would signal the GUI. So the group
  is used only where the child leads one that is not ours, and otherwise the single
  process is signalled exactly as before.

### Added
- **A service can say how long it may take to stop.** *Stop timeout*, on every
  supervised service's form, in seconds. It was eight for everything, which is
  generous for a server that shuts down by closing a socket and nowhere near enough
  for one in the middle of work it must finish — a migration, a module update. Those
  were killed mid-write, which is what made restarting them by hand the only safe
  way to do it. Blank still means eight; `0` means never force it, leaving the
  service *Stopping* until it goes on its own rather than cutting it in half.
  A container has no such field: `docker stop` has a timeout of its own.

### Changed
- **Services & Logs tells its parts from your rows.** Connections and the block for
  logs belonging to no project are filled now, on the tone the table headers already
  use; a project you added keeps the page's own ground. Which is which no longer
  depends on reading the titles. `SURFACE` rather than a step down the grey ramp, so
  the distinction survives dark mode instead of inverting.
- **A page with no projects says so**, on its own dashed ground, where the projects
  would be. It used to show only the parts that are always there, which reads as a
  page that failed to draw — the more so because the unassigned-logs block was shown
  whether or not there were any, purely so the page would not look bare. That block
  now appears when there are such logs, and not otherwise.

## [0.14.2] - 2026-09-04

### Added
- **The scenarios folder is a setting.** *Settings -> Scenarios* says where scenarios
  are read from and written to, the way *Services* and *Log sources* already do. Until
  now a source checkout had only one answer — the checkout — so everything saved on the
  Scenarios page landed among the code. The path travels with every call the GUI makes
  (`--flows-dir`), so the tree the page edits is the tree a run reads.

  Set, it is the **only** tree: nothing that ships with the application is searched
  behind it, so the blocks and `selectors.yaml` your scenarios reference have to be in
  it too. The field says so, and docs/flows.md has the one line that copies them across.

### Changed
- `--flows-dir` no longer requires `--run-tests` or `--recorder`. It says where a tree
  *is*, not what to do with it, and the GUI now passes it on every call — `--describe`
  and the `--flow-*` commands included — so that the tree its Scenarios page edits is
  the tree a run reads. A plain launch simply does not use it. A directory that is not
  there is still refused: it is the only tree, so a wrong path means no scenarios at
  all rather than a quiet fallback.
- The Command page no longer offers `--flows-dir` as a form field; it is
  *Settings -> Scenarios* instead. Two places to name one tree meant the page could be
  editing one while a run read another.
- Example and placeholder text no longer names one particular deployment. The
  project-name box offered "Claim, Helpdesk…" and the service-name box "Odoo Local,
  PostgreSQL DB…"; `users.example.json` carried a real host. They now read
  "Storefront, Billing…", "App Server, PostgreSQL DB…" and `app-dev.example.com`.
  Where a comment reached for a concrete backend it says "a local app server"
  instead. `odoo` stays where it is a *log format* beside `django` and `flask`, and
  the bundled Odoo Debug extension keeps its name — both are what they are, not
  examples.

### Removed
- `run_access_matrix.sh` and `run_access_matrix.txt`, which were one deployment's
  role/access matrix rather than anything this application needs: they named that
  project's logins, its ticket ids and a path into a sibling repository. The same
  run is a `for` loop over `--user` and `--run-tests`, and the README shows it.

## [0.14.1] - 2026-09-04

### Fixed
- A `wait_for_out` or `wait_for_criterion` step with a **blank** value compiled and ran.
  It is the shape a half-filled step actually arrives in — the scenario editor drops an
  empty cell rather than writing it, and the YAML writer renders a missing value as
  `value: ""` — and for `wait_for_out` it was the dangerous one: an empty regex matches
  the first line the service prints, so the step passed instantly having proved nothing.
  A blank value, or a blank service reference, is now refused at compile time, which is
  also what stops the editor saving such a step in the first place.

## [0.14.0] - 2026-09-04

### Added
- **A scenario can call the services it depends on.** Six new step actions —
  `service_start`, `service_stop`, `service_restart`, `wait_for_service`,
  `wait_for_out` and `wait_for_criterion` — act on the services configured on the
  *Services & Logs* page, named as `Project/Service`. So a test that needs a clean
  backend, or that means to prove the app survives a restart, can now say so; until
  now everything a scenario depended on had to be brought up by hand first.

  The two halves of "up" are kept apart, because they answer different questions and
  a backend whose port was already taken answers them differently: `wait_for_service`
  is the process, `wait_for_out` is a regex over what it has printed, and
  `wait_for_criterion` is a criterion already configured on it. `wait_for_out` sees
  only what the service has said since it last started, so a restart cannot be
  declared finished by the previous boot's ready line — not even in the window
  where the restart has been accepted and the old process is still going down,
  which is precisely when the step after it runs. A regex that will not compile
  fails the scenario at compile time rather than becoming a wait that never matches.

  **These steps are carried out by the GUI**, which is the process that owns those
  services — an attached one is its child, so nothing else can stop it. The engine
  asks over the `--events` stream it already writes and is answered on the
  `--control` channel the GUI already opens. Run such a scenario from a bare
  terminal and every service step fails at once saying so, rather than hanging or
  quietly starting a second copy of your backend.

## [0.12.4] - 2026-09-04

### Added
- **The sidebar counts what is live.** *Services & Logs* carries a green point with
  the number of services running and a red one with the number that have failed;
  *Run* carries a point with the number of windows a run still has open. A service
  started on one page and then left behind was visible only from the page that owns
  it — the footer named it in a line it shares with the machine load, and said
  nothing at all about one that had fallen over. The counts are STATUS: the process,
  never a criterion, which is a whole-log assertion and has never spoken for it.
  A count of zero shows nothing, so an idle rail is blank; hovering an entry says
  the same in words. The rail is measured from its names and adds nothing for a
  tally, so the counts arrive without it moving — where a long label leaves no room
  for the figure the entry shows the colour alone, and collapsed the points sit on
  the mark's own corner.
- **Services & Logs totals what it is showing.** Each project header now reads
  `Claim  (1 of 3 running · 1 failed)`, and the page's own line beside the heading
  totals every project — `12 running · 2 failed`. A page of folded blocks otherwise
  answers "is anything wrong" one unfold at a time.

### Fixed
- Reloading the configuration no longer leaves the footer and the sidebar naming
  services that are no longer configured. A service that stops existing reports no
  status on its way out, so the tallies now ask again rather than waiting to be told.

## [0.12.3] - 2026-08-28

### Changed
- **The All buttons on Services & Logs ask first**, and say how much they are about
  to move: how many will be started and how many already are, how many running ones
  will be stopped. A button that names a count — `Start (2)` — was aimed at those
  rows by hand and still acts at once; one that says *All* can take down every
  backend on the machine from a click meant for the row underneath it. Where there
  is nothing for it to do — Stop All with nothing running — it says so instead of
  asking.

## [0.12.2] - 2026-08-27

### Added
- **Services & Logs remembers how it was left.** Every section that folds — each
  project, the unassigned block, the connections — comes back the way it was,
  including after the window is closed and reopened. Folding a block is a view
  preference, so it is kept in the GUI's own settings rather than in
  `services.json`: it is written the moment it happens, needs no Save, and does
  not light one up. A project written by hand still opens on whatever its
  `expanded` key says, until somebody folds it themselves.
- **Every table on the page can be searched by column.** A row of boxes under each
  header, one per column; typing in more than one narrows. What is matched is what
  the table *says*, cell by cell, so the computed columns are searchable too — a
  service can be found by `failed` in Status as readily as by name. A table
  searched down to nothing says that is why it is empty, and shrinks to fit what
  it found. The search survives editing a row it turned up.
- **Newest first.** Projects, services, logs and connections are shown in the order
  they were added, most recent at the top; rows now carry the date they were added
  (`added`) and a copy is stamped afresh. The files keep their own order — so
  nothing about which service starts before which changes with the view — and rows
  written before this keep the file's order, below the ones that say when they
  arrived. `logsources.json` gains the key as well; the launcher ignores it, as it
  ignores every key it does not know.

### Changed
- **Selecting services now means something.** A project's Start, Stop and Restart
  act on the rows selected in it, and say which — `Start (2)` rather than
  `Start All`. Selection took no modifier key and did nothing before, which made
  it look like a feature that had stopped working. With nothing selected the
  buttons still take the whole project, the page's own Start All / Stop All still
  take everything, and a service brings up whatever it waits for whether or not
  that was selected too.

## [0.12.1] - 2026-08-27

### Changed
- **A criterion outlives the run it describes.** Stopping a service was clearing
  its criteria, on the reasoning that a green `start` beside a stopped service is
  a claim about a run that is over. True, and the wrong trade: stopping is when
  what the log said matters most. A service whose whole job was one run has
  finished by the time anybody looks at it, so clearing on stop threw the answer
  away at exactly the moment it was wanted — and how far a crashed one got before
  it died is the useful half of the crash.

  They are cleared when the service *starts*, and only then, so a tag describes
  the last run until the next one begins.

## [0.12.0] - 2026-08-26

### Added
- **The GUI can start the backends whose logs it was already reading.** The Log
  sources page is now **Services & Logs**, organised by *project* rather than
  by kind: each project is a block holding its services above its logs.

  The page could say which backend logs a run may stream; it could not say whether
  the backend was even running, because nothing here had ever started one. A local
  Odoo, its Postgres container and its log file are one thing to the person using
  them and were three unrelated facts on screen.

  A service is a Python script, a shell command, a Docker container or a Compose
  file — each one row in `runnertypes.TYPES`, with its command lines and a field
  spec the form is generated from, so a new kind needs no dialog of its own. What
  is *not* cosmetic is who owns the running thing: a supervised service is a
  process we started and its state is that process's state, while `docker start`
  exits the moment the daemon has the job, so a container's state has to be asked
  for rather than assumed.

  **A service can say what has to be up before it is.** *Starts after* names other
  services in the same project; Start waits until each of them reports running —
  the row says *Waiting…* and what it is waiting for — and Stop takes them down in
  the reverse order. The order is read off the dependencies rather than off the
  list, so a project whose services were typed in any order still starts in the
  right one. A dependency that cannot start says so on the row that was waiting
  for it, rather than leaving it waiting forever. A loop is refused with the ring
  named, and is broken rather than recursed into if a hand-edited file has one.

  Each service carries **Detach allowed**. Off, it is a child of this window, and
  closing the application names what will stop and asks first. On, it is started
  detached and found again by pid next time. The confirmation is the mechanism and
  not a courtesy: PySide6 binds no `setChildProcessModifier`, so there is no
  `PR_SET_PDEATHSIG` behind it — and because Qt kills a running child when its
  `QProcess` is destroyed, a service allowed to detach is started through
  `subprocess.Popen` instead, writing to a file the console reads.

  Services live in a new `services.json` under your own directory
  (`~/ChromeMultiSession`), with the path settable in **Settings**. The launcher
  neither reads it nor needs to — which is exactly why where it goes is nobody
  else's business. It first went beside `logsources.json`, and that is wrong in
  the case that matters: from a source checkout the launcher's config path *is*
  the checkout, so the GUI's own file landed in somebody's repository. A file
  still at the old location is read when there is nothing at the new one, and the
  next Save moves it; the old one is left alone rather than deleted.

- **A service can be told what to watch its own log for.** *Criteria* on a
  service: a name you choose (`start`, `finished_tests`, anything), a colour, and
  the rules that light it. One that matches shows as a tag beside the service, in
  its colour; the ones that have not are on the tooltip rather than in the row,
  which would otherwise be mostly grey words about things that have not happened.
  A project that uses none has no such column at all.

  "Running" has only ever meant that the process started, which is the weakest
  useful claim: an Odoo whose port is taken is Running, and so is one that booted
  cleanly. What separates them is in the log, and nothing read it.

  The rules are about the **whole log**, not one line — which is what
  `grep "started" && grep "!ERRORS"` actually asks. A *must contain* rule is
  satisfied by any line at any point and stays so; a *must not contain* rule holds
  until the first line trips it, and then permanently does not. So a criterion can
  go dark again: `start` stops being true the moment a `CRITICAL` line arrives.
  Cleared when the service starts *and* when it stops or falls over, so a tag
  only ever describes a run that is happening.

  A criterion reads the service's own output by default, or a file if you name
  one — which is what a backend started with a logfile needs, since it prints
  almost nothing to its console.

  Deliberately display-only: a criterion never changes the status, never holds up
  anything that waits on the service, and never stops it. STATUS means the
  process and this means the log, and neither pretends to be the other.

### Changed
- **A log can name the project it belongs to.** One optional `project` key per log
  in `logsources.json`, which decides only which block it appears under.
  `engine.serverlog` already sweeps keys it does not know into `LogSource.extra`
  and ignores them, so this is not a format change: `--server-log`, `--describe`
  and every existing file behave exactly as before, and a log naming no project
  is an ordinary log shown under *Unassigned*. Nothing needs migrating.
- The sidebar entry is renamed to **Services & Logs**. Its internal key is
  unchanged, so a hidden-sidebar setting or a remembered last page still resolves.
- **Save only offers itself when there is something to save**, and closing on top
  of an unsaved edit asks first. Folding a block is not counted as an edit: it is
  a view preference, saved along with the next real change.
- **A Browse button can now reach the paths that live in dotted directories.**
  A project's interpreter is `.venv/bin/python` and an ssh key is in `~/.ssh`, and
  a file chooser lists neither. It does show what is inside a dotted directory it
  *opens in*, though, so every chooser now starts where the answer is likely to
  be rather than above it. Better still for the commonest case: a Python service
  finds the project's own `.venv` by itself, and the Interpreter field shows what
  leaving it blank will actually run.
- Buttons that live in a table row no longer paint the accent on top of the
  accent. A row that carries its own buttons marks selection with a quiet band
  instead of the accent flood: a widget in a cell paints its own background and
  cannot be told its row is selected, so the flood left the buttons stranded on a
  rectangle of the wrong colour, and no ink was legible both on and off it.
- A selected row can be un-selected by clicking it again. Open Tail and Open Full
  are aimed by selecting a log, so there has to be a way to aim at nothing — and
  in a list of one there was none.
- Buttons that act on services are dark while a project has none, and the paths of
  the two files the page edits moved to **Settings**, where the other answers
  about where things live already are.
- **Where `logsources.json` lives is a setting too**, defaulting to
  `~/ChromeMultiSession` like `services.json`. The launcher resolved it against
  its own data root, which from a source checkout *is* the checkout — so the file
  landed in somebody's repository, the same problem `services.json` already had.

  It differs from that one in a way that matters: this file is not the GUI's
  alone, `--server-log` reads it. So the path travels with every call the GUI
  makes into the core, through a new **`--log-sources=FILE`** flag — otherwise
  the file being edited and the file a run reads come apart, and the page's own
  Open Tail / Open Full read the wrong one. The plumbing was already there: every
  function that touches the file took a `path` argument and nothing on the
  command line set it.

  A file still at the old location is read while it is the only one there, the
  page says so, and the next Save writes the new one; the old is left alone
  rather than deleted. The core is pointed at whichever one is actually in force,
  so the two never disagree even mid-migration.

- **Every table header is a filled band.** They were painted in the page's own
  colour, which makes a header not a band at all but small grey text floating
  above some rows — and the Services & Logs page carries four tables, so it read
  as one undifferentiated field with words scattered through it. Filling them caps
  each table and is what says where one ends and the next begins. Applies
  everywhere rather than on that page alone: a header that meant one thing on
  Services and another on Credentials would put the confusion back.

  The selected row moved one step further off the page at the same time, so a
  selection and a header can never be mistaken for one another.

### Fixed
- **Where `services.json` will be written no longer hides a reason not to write
  it.** Reading from the old location and saving to the new one is housekeeping,
  and it was announced *after* validation — so over a file that was not valid
  JSON it replaced the one message that had to be read with the one that did not
  matter yet, on a page whose Save was already correctly dark.
- **The status bar names what is running** — `odoo: running` — wherever you are in
  the application. A service started and then navigated away from was otherwise
  invisible: nothing outside its own page said it was still up. Past three it
  counts the rest, because that line is shared with the machine's load and the
  worker limit; the whole list, with the project each belongs to, is on the
  tooltip.
- **An empty table says what it means.** A header over a blank band reads as
  something that failed to load rather than as something not configured yet, and
  this page can carry four of them at once. Each now says what it is waiting for
  — no services, no logs, no connections, nothing watched for — following the
  table's own model, so filling one is not something anybody has to remember.
- **A form opens at the height it needs.** Every hint under a field is a
  word-wrapped label, and one of those reports a size for a width it has not been
  given — always more lines than it takes. Adding those up left a band of nothing
  under the last field of every dialog; they now take back whatever they did not
  use, once they have real geometry.
- **The splash opens in the colours the window will.** Its status strip filled
  with the ink and wrote in the background, so it was the negative of whatever
  was about to appear: a light bar under a dark window, and a dark one under a
  light window. The first thing anybody sees should not contradict the second.
- **Table headers are filled.** They were painted in the page's own colour, which
  is not a band at all — small grey text floating above some rows — and a page
  carrying four tables read as one undifferentiated field with words scattered
  through it. Each header now caps its table: a tint darker than the page in
  light mode, lighter in dark, since the neutral ramp inverts with the mode. The
  selected-row band moved one step further out so the two can never read as each
  other. Every table in the application, because a header that meant one thing on
  one page would be worse than none.
- The Environment grid and *Starts after* list are as tall as what is in them.
  A fixed maximum is not a height: an empty grid and a one-item list each took
  the whole of it, and left a field that was mostly nothing.
- Every line a service prints now goes through one place. A detached service's
  output was appended and emitted *beside* that door rather than through it, so
  anything watching output would have missed it entirely — which the criteria
  above would have shipped as a silent half-working feature.
- The status bar no longer names the core script and the interpreter. Both are
  decided once in Settings and are on About; three fixed strings for facts that
  cannot change while the window is open were taking its left half. `config:`
  stays — it is the one of the three that changes what a run reads.

## [0.11.0] - 2026-08-25

### Added
- **A whole row can be recorded, and named by its number.** Two things were missing
  and neither works without the other.

  `P` selects the element *around* the one you picked, and `C` goes back in. A pick is
  whatever was under the pointer, and in a table that is always a cell — cells fill the
  row, so there is no point on screen where the pointer is on the `<tr>`. A step about
  a whole line could not be recorded at all, only hand-written afterwards. The menu
  names what it will move to (*select the tr around it*), and the highlight now stays
  on whatever the menu is about, so `P` is something you watch rather than guess at.

  `N` writes the selector as `:nth-match(base, index)` — offered whenever the element
  is one of several, with the count in the menu (*the 3rd of 12*). This is the one that
  needs Playwright's own selector engine: `:nth-of-type()` counts siblings of a tag
  inside one parent, so from a cell it counts the **columns** beside it. A recording
  that means "the third line" came out as `[name="shipment_number"]:nth-of-type(4)`,
  which is the fourth *field*, matches one cell in every row, and acts on the first.
  `:nth-match(tr.o_data_row, 3)` counts matches, which is what "the third line" means.

  Together: hover a cell in the third row, `P` to the row, `N` to count it, `1` to
  click. The `matches more than one element` warning now says which key narrows it.

### Fixed
- **A cell in a list row is a cell, not the row's checkbox.** Picking anything in an
  Odoo list came back as that row's record selector: the menu offered *check it IS
  selected*, *wait until it becomes selected* and a *select it* that recorded the
  checkbox — and `click`, the one step wanted, was not offered at all.

  The recorder looks around the element it was given on purpose, because a radio is a
  13px circle and people click the label beside it, which in Odoo is the input's
  *sibling*. What it used to accept was "the only checkbox in some ancestor", and
  every row of a list holds exactly one. Now an input found that way counts only when
  the picked element is what **labels** it — `for`, a wrapping `<label>`, or
  `aria-labelledby`. An option is a control and its name drawn as one thing; a record
  selector has no name, so it is reached the honest way, by picking the input. The
  same bound applies inwards: a checkbox inside what was picked has to be within a
  few generations of it, so a panel that happens to contain one somewhere is not that
  checkbox either.

  Whatever was picked can now also be clicked as itself. Clicking the cell a record
  selector sits in is not ticking the record selector, and both are offered — except
  on the input itself, where *select it* is already that click. Which of the two leads
  depends on how far in the control sits: a cell drawn around a checkbox *is* that
  checkbox and leads with it, while a row two levels out is a row and leads with
  itself.
- **`checkbox-comp-2` is not a name.** Owl numbers its components in render order, so
  an id of that shape is green once and by luck. Recognised as a render counter now,
  the same way `o_field_12` and bare numbers already were.

## [0.10.0] - 2026-08-21

### Added
- **The sidebar collapses, and you choose what is on it.** 196px of labels is a
  lot to give a navigation that is read once and then known, so `Ctrl+B` — or
  *View → Collapse sidebar*, or the handle on the rail itself — folds it down to
  its marks, each carrying its label as a tooltip. The group headings go with the
  labels, because "CONFIGURE" cannot be drawn in the width a mark needs and a
  clipped word is worse than none.

  *View → Sidebar items* switches each of the ten entries on or off, for the
  common case of using three of them. What is stored is what is **hidden**, so a
  page added in a later version arrives on the rail rather than having to be found
  and switched on. It will not empty the rail — the last entry refuses to go — and
  it will not leave you on a page whose entry you just switched off. Both settings
  survive a restart, and neither overrides developer mode: Command shows when that
  mode is on *and* its entry is switched on.

### Changed
- **Flag names are gone from the pages unless developer mode is on.** `--flows-dir`
  is noise to someone launching sessions and the only thing worth knowing to
  someone about to type it, so a string that names a flag is now written twice and
  the mode picks: "Flows" and `--flows-dir`, "Refresh" and "Refresh --describe".
  Nothing is hidden either way — the plain wording says what the control does, not
  less. The Command page is exempt, being the command line itself.

### Fixed
- **The URL override straddled its own row.** The design's inputs are 30px and a
  row of text is 30px, but a table insets a cell widget by the item padding on
  both sides — so the editor was handed 24px, rendered at its own minimum anyway,
  and the extra hung downwards across the gridline below. Rows are now measured to
  fit the editor they hold.
- **The Server log box was 150px narrower than the card around it**, for a button
  that shares its title's line and none of its own. A folding section is a header
  and a body stacked in one widget, and standing the whole thing beside a control
  takes that control's width off both.
- The Tools menu's *Create a starter users.json* opened a window titled
  `--init-users-json`.

## [0.9.1] - 2026-08-21

### Fixed
- **The recorder wrote selectors no browser would parse.** An element whose `id`
  starts with a digit came out as `#[id="49"]` — the `#` form and the attribute
  form glued together — and the run failed on it with "Unexpected token #". The
  id was worthless even spelled correctly: Odoo's search dropdown numbers its
  rows from a counter that starts over on the next render, and only runs of four
  digits or more were being rejected as generated. A purely numeric id is no
  longer taken for a name, so those rows now record by their structural path
  instead. Recordings that already contain such a step have to be made again;
  there is nothing in `#[id="49"]` to translate.

## [0.9.0] - 2026-08-20

### Added
- **The backend's log, tied to the window it belongs to.** Ten windows open as ten
  roles, one misbehaves, and the server's log is a single stream with everyone's
  requests mixed together — so the evidence that would explain the failure was the
  one thing the tool could not show. `--server-log` now tails that stream and gives
  each session only the lines written after **its own window opened**: live in the
  GUI's session panel, and - under `--run-tests` - in the report beside the
  screenshots, one file per log covering that scenario's own window. Turning the
  streaming on is the whole decision: it is written pass or fail, whatever
  `--report-*` says, because streaming a backend's log all run and then throwing
  the evidence away is not a thing anyone wants a flag for.

  Where the logs live is described in a new `logsources.json`, beside `users.json`
  and git-ignored for the same reason. It has two levels because one machine
  usually serves several logs: a **connection** says *where* to run a reader
  (`local` or `ssh`), a **log** says *what* to read there (`file`, `docker`,
  `journal`, `http`) and which environments it belongs to. One connection serves
  every log on a machine, so a stand with three logs opens one ssh connection
  rather than three. `--server-log=list` prints what is configured, and
  `--server-log-show=NAME` reads one (`--server-log-lines=N|all`), which is how an
  unknown host key or a stopped container gets found before a run depends on it
  rather than after the report comes back empty - and, unlike a yes/no check,
  shows whether it is even the right file.

  Each session's panel folds out its own lines, and **Separate Window** moves them
  into a full-size window with a search and a level filter - several at once, so ten
  roles can be read side by side. The strip stops drawing them while a window has
  them, and pulses instead of going blank.

  Levels are the log viewer's own five - `DEBUG`, `INFO`, `WARN`, `ERROR`,
  `CRITICAL` - not the HUD's three. Folding "the process is going down" into the
  same bucket as "that request failed" is right for one small in-page widget and
  wrong for the place you go to read a log. They are coloured by severity, the
  filter is a threshold rather than a single level, and the palette is read from the
  theme per line so it follows dark mode.

  Correlation is by time and says so: a session sees what was written after it
  opened, which separates environments and runs but cannot separate two windows
  clicking at once against the same stand. The matcher is a strategy object so a
  precise key can replace it without touching the reading or the fan-out.

  **No debug port is involved.** Drawing this *inside* the page would have needed
  `--remote-debugging-port`, which is unauthenticated and would have handed
  anything on loopback control of a real logged-in profile. Nothing in this feature
  talks to the browser, and a plain launch still opens no port.
- **A Log sources page in the GUI**, next to Credentials. Connections and logs are
  created and edited in a form, not in the grid: the fields that matter depend on
  choices made in the same row - an ssh connection needs a host and a local one must
  not have one, a log's target is a path, a container, a unit or a URL depending on
  its kind - and a form can show exactly what applies and explain it, where eight
  narrow columns in a fixed order cannot. The tables are the overview, with Edit,
  Copy and Delete on each row. Validation mirrors the launcher's own, so the editor
  cannot write a file the next launch would refuse, and Test asks the core whether a
  log can really be read - **Open Tail** and **Open Full** put it on screen, in a
  window that filters and saves and is not modal, so two logs can sit side by side.
  Launch Sessions gained a matching **Server logs** block, filtered to the
  environment being launched.
- **Format presets named after the shape of a line, not after an application.**
  `iso`, `slash`, `clf`, `syslog` and `none`, with `django`, `fastapi`, `node`,
  `nginx`, `apache`, `go`, `rails`, `odoo` and the rest accepted as aliases for the
  shape they write. `iso` alone covers everything using Python logging's default
  `%(asctime)s`. Anything no preset describes is served by giving `timestamp` and
  `level` patterns on the log itself - which the GUI now offers as "custom" rather
  than leaving to a text editor.

### Fixed
- **A backend logging UTC streamed nothing, silently.** Odoo (and plenty else)
  writes UTC; on a machine that is not, every line parsed hours into the past, fell
  outside every session's window, and the run produced an empty panel and no report
  file - which looks exactly like a server that had nothing to say. A line coming
  off a live tail was written moments ago, so a timestamp claiming otherwise is a
  misread one: it is now read as written-now, and the mismatch is reported once with
  the offset measured and the fix named. That fix is a new `tz` on the log
  (`local` / `utc` / `+HH:MM`), which applies to the presets and not only to a
  hand-written pattern - the shape of a line and the clock it was written by are
  different questions, and only the second changes per deployment.
- **Server logs never reached the report.** `run_scenarios` took the hub and
  `_run_scenario` used it, but nothing carried it between them, so every real run
  wrote no `server_log-*.log` at all - while both halves passed their own tests.
  The gap was the test suite's: it covered each end of the path and never the path.
  It now drives the public entry point. `--server-log` naming a log the chosen
  environment does not have - which is what a saved configuration does the moment
  the environment is switched - exited before a single window opened. So did a
  `logsources.json` with a mistake in it, a custom pattern that would not compile,
  and combining it with `--detach` (where the launcher exits at once and takes its
  reader threads with it, so nothing could be streamed anyway). A backend log is a
  diagnostic, and a diagnostic that prevents the thing being diagnosed is worse
  than none: each of these is now reported once and costs that one log. Asking for
  two logs and misspelling one no longer costs the other either. The GUI does not
  offer the `--detach` combination at all, and says why rather than dropping it
  quietly.
- **The editor destroyed a custom log format.** `timestamp` and `level` - the
  patterns that make any backend readable without a preset - were parsed and then
  dropped when the file was written back, so opening the Log sources page once
  silently replaced a hand-written format with whatever preset the combo happened
  to show. They now round-trip, and a pattern that will not compile, or that
  captures nothing, is refused before it can be saved.
- **A log file rewritten in place lost a line.** The follower spotted a truncation
  by watching the file's size, and there is no moment to observe between a truncate
  and the write that follows it — by the next poll the file was already longer than
  the old read position and looked like ordinary growth, so reading resumed from a
  stale offset and handed out the tail of a line as though it were a line. It now
  fingerprints the file's first bytes, which a rewrite changes however fast it
  happened, and checks that *before* reading rather than after.
- **A follow command that died looked like a log with nothing to say.** An unknown
  ssh host, a container that is not running and a missing path all end the reader
  in milliseconds; it simply returned, so every one of them read as silence — the
  worst possible answer for a "test this connection" button. The reader now reports
  why it stopped, preferring the line that says what went wrong over ssh's leading
  warning about an identity file.

## [0.8.3] - 2026-08-19

### Fixed
- **Saved Launch Sessions configurations could vanish.** The store read its file
  as strict UTF-8, so a byte-order mark - which is what a Windows shell writes
  when told "utf8" - made every configuration read back as none at all, and the
  next save then wrote that empty default over the file. Reads now tolerate a
  BOM; a file that still cannot be parsed is copied aside before anything
  overwrites it; and saving is read-modify-write, so one window's save no longer
  erases what another window saved.

## [0.8.2] - 2026-08-18

First release built and run on Windows. Everything here is a Windows-only fault
that the Linux build never had - each one was found by installing the thing and
using it, which is the only way these could have been found at all.

### Fixed
- **Runs never started.** `cms_gui.runner` called
  `QProcess.setCreateProcessArgumentsModifier`, a Qt method PySide6 does not
  bind, on the Windows branch of every launch. The `AttributeError` landed
  before `proc.start()`, so the run was recorded as started, the page said
  "Launching...", and no process ever existed. The modifier is now applied only
  where it exists; losing the process group costs the graceful stop, never the
  run. `stop()` no longer signals a process group that was never created.
- **Extensions were never installed** - including the auto-login one, which is
  the point of the program. Chrome's tamper protection strips an
  `extensions.settings` entry written from outside the browser, and
  `--load-extension` is refused (137+) with or without
  `DisableLoadExtensionCommandLineSwitch`. Windows now installs the planted
  directories over CDP with `Extensions.loadUnpacked` and reloads the tab so the
  content script runs. See the `--extensions` section of the README for what
  that costs: a loopback debug port on every launch.
- **Chrome detection opened browser windows.** `chrome.exe --version` does not
  print a version on Windows - it starts the browser - so every `--describe`
  launched one window per candidate path and still reported no version. The
  version is now read from the executable's own version resource, and duplicate
  candidates (the `App Paths` key and `%PROGRAMFILES%` name one file between
  them) are probed once. `version.dll` is loaded by absolute path, because
  PyInstaller redirects a bare library name into the bundle, where our own
  `VERSION` stamp file matched it.
- **First launch of an installed build failed with WinError 267.** The GUI
  spawns the core with the user's data directory as its working directory, and
  nothing had created it yet; a working directory that does not exist stops the
  process from starting at all.
- **The build script could not finish on Windows.** `Set-Content -Encoding utf8`
  writes a BOM in PowerShell 5.1, which `json.load` rejects, and `pip.exe`
  cannot upgrade itself.

### Changed
- **Every icon in the interface is drawn, not typed.** Each mark used to be a
  character picked at runtime from a list of candidates, by asking the resolved
  body font whether it could draw it. On Windows the answer was almost always
  no, so the interface fell back to its ASCII stand-ins: a navigation rail
  reading "= o > +", a toolbar offering "% Developer mode" and "* Settings", and
  a step tree marking passes with "+". They are now painted from vector
  primitives (`cms_gui/icons.py`), so they render identically whatever fonts are
  installed - the same set, at every size, in both light and dark palettes. The
  rail is built from tool buttons because a push button centres an icon and its
  label as one block, which would let each mark drift with the width of the word
  beside it.
- **The executables carry the application's icon.** Both PyInstaller specs now
  embed `icon.ico`, and the build renders it *before* freezing rather than
  after - without that, PyInstaller embedded its own default, which is the
  Python logo, and that is what Windows showed in the taskbar, in Explorer and
  on every shortcut the installer created. The `.ico` is now assembled here as a
  genuine multi-size file (7 images, each painted at its own size) instead of
  one 256px image left for Windows to scale down to 16. The GUI also claims an
  explicit AppUserModelID on Windows, without which the taskbar attributes the
  window to whatever launched it - python.exe, from a checkout.

### Added
- **The installer asks where your project folder goes** - scenarios, reports,
  sessions and `users.json` - and writes it to `<InstallDir>\cms.ini`, which
  `runtime_paths` reads after `$CMS_HOME` and before the `%USERPROFILE%`
  default. The choice is remembered across upgrades, and uninstalling never
  touches the folder.

## [0.8.1] - 2026-08-18

### Added
- **`--jobs` now means windows, not drivers.** With `--close-after` the launcher
  no longer opens every window up front: each Chrome is started inside the slot
  that will drive it and closed when its scenarios end, so eight accounts at
  `--jobs=2` means two resident browsers rather than eight. Before this, `--jobs`
  only staggered the stepping while every window stayed open and resident, which
  is why lowering it freed nothing.
- **`--jobs=auto`**, the one value that lets a load governor move the number
  while the run is under way. It starts at one window per core and steps down on
  memory headroom or on the kernel's own stall figures (`/proc/pressure`, PSI),
  not on CPU utilisation - a rig driving windows on every core *should* read
  100%, and tripping on that is what ratchets a ceiling to one and leaves it
  there. A step taken for CPU has to prove it helped, or it is undone and CPU
  stops being a trigger for a while. A number you type is never moved.
- **Stop one window.** Stop carries a menu of the windows still running; picking
  one stops driving it and closes it while the rest of the run carries on. The
  launcher accepts it on stdin via the new `--control=-`, the inbound half of
  `--events=-`.
- The Run page shows **every scenario a window has run**, each with its own steps
  and outcome, instead of only the one running now. The in-page overlay always
  showed the whole list; the two now agree.
- The launch page's summary footer **folds away**, and its rarely-used options
  live behind one unlabelled button rather than beside Save and RUN.
- The recorder panel **edits its own steps**: delete one, move it up or down,
  or retarget it. A bad capture is obvious while the page is still on screen and
  much less so afterwards. Python owns the list, so the panel sends an intent and
  repaints from what comes back - it never edits its own copy, which is what
  keeps the two from disagreeing after a navigation.
- The panel **collapses to its header**, and fades to 35% while collapsed so it
  stops covering the app; hovering brings it back. The choice is remembered in
  `sessionStorage`, because a navigation replaces the whole renderer.

### Changed
- **A stopped run always closes its windows**, whatever `--close-after` says.
  That flag answers what happens when a run *finishes*; someone who pressed Stop
  is not asking to be left with seven browsers on a half-finished flow.
- **Stop lands between steps**, not between scenarios. A forty-step flow whose
  remaining steps each time out at 30 s took twenty minutes to reach the next
  scenario boundary, and for all of it Stop looked like it had done nothing.
- **`--recorder` records one window.** Several would each show their own panel,
  only the first would get the scenario id that was asked for, and a person can
  only be clicking in one of them. Pick the account with `--user` or
  `--filter-users`; the GUI asks which one.
- A session that failed a scenario **no longer reports PASS** because a later one
  passed - in the Run page and in the in-page overlay both. The banner spoke for
  whichever flow ended last, above a tree with a red mark still in it.
- The Run page settles when the **scenarios** end rather than when the launcher
  exits. Without `--close-after` the launcher stays up holding the windows open,
  so the page sat on RUNNING with a ticking clock long after the last step.
- Closing the window during a run warns with buttons that say what they do, and
  waits long enough for the launcher's graceful teardown to finish.
- **The recorder shows itself.** A window launched with `--recorder` carries the
  panel from the moment it is attached - no right-click, no menu item, nothing to
  find. That is not capturing automatically: nothing becomes a step until Capture
  Step is pressed. It removes the bundled extension entirely, along with its
  `contextMenus` permission, its per-profile install and the DOM flag the two
  halves talked over.

### Fixed
- The GUI's memory readout computed `100 * (1 - available) / total` instead of
  `100 * (1 - available/total)`, reporting a large negative percentage - and
  never tripping its own warning threshold. The `/proc` reader now lives in one
  place (`system_load.py`, mirrored for the GUI) instead of three copies.
- The launch page no longer **edits your saved configuration behind your back**:
  it used to wind the jobs number down every two seconds the CPU was busy, which
  is exactly while a run is going.
- A `QThread` outliving its window took the process down on exit.

### Removed
- `extensions/_recorder/`, which existed only to carry a right-click into the
  page.

## [0.7.0] - 2026-08-15

### Added
- **Checking which radio or checkbox is on.** None of the assertions answered
  "is this option selected?", which is most of what a permissions screen is. CSS
  already says it - `:checked` - so this needs no new step type, and the tree
  writes selectors that way by hand already
  (`flows/selectors.yaml`: `roles_wizard_agent_checked`). Picking a radio now
  offers *check it IS selected*, *check it is NOT selected* and *wait until it
  becomes selected*, and finds the input behind whatever you clicked: the label
  beside it, or the row around it - in Odoo the label is the input's sibling, not
  its parent. `data-value` joins the attributes synthesis looks for, because
  without it every radio in a group looks identical: they share their `name`.

### Fixed
- The recorder could produce `click: "a"` - every link on the page, recorded as a
  step. Synthesis gave up after four ancestors and returned whatever it had,
  which for a plain `<a>` in an unremarkable list is the tag on its own. It now
  walks further, skips ancestors that describe nothing, and falls back to
  `:nth-of-type()` rather than to something meaningless. When the result still
  matches more than one element the menu says so in warning colour, since a step
  like that acts on whichever element Playwright reaches first.

### Added
- **Continue a recording.** RUN ▾ → *With Recorder* works out for itself what to
  write to. With a scenario selected it asks - *Continue "x"* / *Start new* /
  *Cancel* - because appending to a scenario and replacing one look identical
  until it is too late. With nothing selected it asks nothing and records a new
  one. Continuing loads the steps the file already has, shows them in the panel,
  appends what you capture, and keeps the name, description and tags it carried,
  because those are edits somebody made on purpose. `--recorder=ID` does the same
  from a shell.
  What counts as selected: whatever is open in the Scenarios editor, or a single
  scenario chosen on Launch Sessions. Never one that ships with the application -
  it cannot be written back, so recording into it would collect steps and then
  throw them away.

## [0.6.1] - 2026-08-15

### Fixed
- **`fill` never saved a step.** It asked for the value with `window.prompt`, and
  the recorder is driven over CDP - where Playwright dismisses a page's dialogs
  by default. `prompt()` returned null, the step was dropped, and nothing said
  why. Values are asked for in the recorder's own panel now, so no dialog is
  involved; the same goes for `select` and `assert_text_contains`. The
  end-to-end test had missed it by stubbing `window.prompt`, which tested around
  the bug rather than through it - there is now a test that reads the source and
  refuses any dialog call.

### Added
- **Match by exact text** (`T` in the action menu), for picking one row out of a
  list by the data in it - a customer, a reference, a number. Still never the
  default and never for a UI label: those are translated and the step would
  break on the next environment. Data is not, which is why this is offered at
  all, and Playwright's `:text-is()` makes it exact.

## [0.6.0] - 2026-08-15

Scenarios stop being something you write in a text editor. There is a page for
them, a tree of your own to write them into, and a recorder that captures one
from a window you are already working in.

### Added
- **A Scenario Recorder.** RUN ▾ → *With Recorder* opens the windows as usual,
  with one addition: right-click any of them and there is **Start Scenarios**.
  From then on the page carries a recorder panel, and capture is explicit -
  moving the mouse, typing and every intermediate input event are ignored. Press
  **Capture Step**, hover until the element you want is outlined, click it, and
  choose from the actions that element can take. That is one step, and nothing
  else is. The action is performed as well as recorded, so the page advances
  exactly as the replayed flow will; assertions are recorded without acting.
  Finish writes an ordinary scenario, through the same validator everything else
  goes through, tagged `template` because a recording is a draft.
- The recorder works inside a dropdown, an autocomplete list or any other
  transient popup, which is where most of what is worth recording actually
  happens. Three things make that possible: **F2** arms it, so opening capture
  mode is not a click somewhere else; while armed, mousedown and pointerdown are
  taken away from the app as well as the click, so its own "close on outside
  press" never fires; and the action menu is chosen with **1-9 / ↑↓ + Enter**,
  because a click on the recorder's panel is an outside-click as far as the app
  is concerned. Clicking still works everywhere it is safe.
- The recorder prefers a name already in `selectors.yaml` over anything it could
  synthesize, so a recording reads like the flows beside it and follows the tree
  when that name is re-pointed. Failing that it uses a structural attribute -
  `data-menu-xmlid`, `name`, a stable id - and **never** a visible-text selector:
  the same app renders Ukrainian on one environment and English on another, so a
  selector keyed on a label is the one thing guaranteed to break.
- `--recorder[=ID]`, and a small bundled extension (`extensions/_recorder/`) that
  is what puts *Start Scenarios* in the context menu. It is installed only under
  `--recorder`, and so is the debug port the recorder attaches through - a plain
  launch is unchanged, and an unauthenticated port is not something to open by
  default.
- **A Scenarios page** in the GUI: every scenario and every block, a step editor
  and a YAML view of the same file, plus New, Duplicate, Import, Export, Delete
  and Revert. A step's target is an alias - another flow's id, or a name from
  selectors.yaml - so the page shows what each one resolves to and can follow it:
  `use: access.open_app` opens that block, a named target leads to the selector
  it stands for. `selectors.yaml` is editable too, with the shipped names listed
  read-only and "Add to my selectors" taking a copy to override.
- **A writable flows tree.** Flows now resolve against a search path: your own
  `~/ChromeMultiSession/flows` first, then the one that ships with the app. A
  scenario of yours shadows a bundled one with the same id and can still `use:`
  the shipped blocks without copying them; `selectors.yaml` is merged rather than
  replaced, so re-pointing one name after a UI change does not mean taking the
  whole file. `--flows-dir` still means exactly the directory it names - only the
  default is layered - and in a source checkout both trees are the same directory,
  so nothing about development changes.
- Core commands for scenario files, since the GUI depends on PySide6 and nothing
  else and cannot read or write YAML: `--flow-show`, `--flow-save --from=FILE`,
  `--flow-delete`, `--flow-import`, `--selectors-show`, `--selectors-save`. All
  answer with JSON and exit, like `--describe`, and nothing is written unless it
  compiles first.
- `--describe` now carries the blocks, the merged selector map, and per scenario
  which tree it came from and whether it can be edited - plus the step grammar
  itself, so an editor's action menu cannot drift from what the compiler accepts.

### Fixed
- `describe()` defaulted `flows_dir` to the bundled directory before the loader
  saw it, the same way the compiler and the runner did. That hid every scenario in
  the user's own tree and reported all 53 bundled ones as editable.

## [0.5.0] - 2026-08-14

The first release that installs. Everything below had been sitting in
`[Unreleased]` since 0.4.0; cutting it is what gives the `.deb` a number to carry,
and `cms_gui.version()` now reads that same number, so the GUI's About box, the
core's `--version` and the package all answer alike.

### Added
- **A standalone installer for Ubuntu 22.04** — `sudo apt install ./chrome_session_amd64.deb`
  and the app is in the applications menu, with no Python, pip, git or virtualenv on
  the machine. Both halves are frozen with PyInstaller (`packaging/pyinstaller/`) and
  wrapped in a `.deb` by `packaging/build_deb.sh`, which writes versioned output to
  `installers/linux/<version>/`. Playwright's Node driver is bundled — `connect_over_cdp`
  goes through it — but no Chromium is: the app still attaches to the Google Chrome
  already installed. See [packaging/README.md](packaging/README.md); Windows is
  planned there and the runtime work it needs is already in place.
- `runtime_paths.py` — one place that answers where things live, splitting the
  resources that ship with the app (`flows/`, `extensions/`, `hud.js`) from the data
  that belongs to the user (`users.json`, `user_sessions/`, `reports/`). Installed,
  the second half is `~/ChromeMultiSession`, so an upgrade cannot touch a session,
  a credential or a report. **In a source checkout both still resolve to the checkout,
  exactly as before** — nothing about working on the project changes.
- `--describe` now reports `chrome`: the browser the core found, its version, and a
  message saying how to install one when there is none. The GUI shows it once at
  startup, so a missing browser is a sentence rather than a failed launch.
- GUI: `cms_gui.version()`, resolving the release number the same three ways
  `session_launcher.version()` does — an installed distribution, the `VERSION` file a
  packaged build carries, then `pyproject.toml` in a checkout. About names both the
  GUI's version and the core's, and says which file the core one came from; the core
  is asked with `--version` when no `--describe` has succeeded yet.
- GUI: Launch Sessions marks unsaved work — both Save buttons turn red once the
  controls differ from the configuration they were opened from, and settle again if
  the edit is undone by hand.
- **A desktop GUI** in `gui/` (PySide6, its own virtualenv, `python3 gui/bootstrap.py`):
  environments, a `users.json` editor, a builder covering every launcher flag, and a
  live run view — step tree, log stream, session lifecycle and artifacts. It never
  imports the core; it spawns `session_launcher.py` through a configured interpreter,
  so the two environments stay independent and the launcher remains the single source
  of truth.
- `--events=-|FILE` — a structured JSONL event stream for programs driving the
  launcher: windows launched, CDP attached, every step, artifacts written, run summary,
  windows exited. `-` writes to stdout, which stays free because log records go to
  stderr. `engine/events.py` also provides the observer that feeds it, fanned out
  alongside the in-page HUD by `engine.events.Tee`.
- `--describe` — the whole inventory as JSON on stdout: environments, users (with
  `has_password`, never the password), scenarios with tags and whether
  `--run-tests=all` would run them, extensions, and the values each flag accepts. A
  broken config degrades to `warnings` inside the payload rather than a plain-text
  exit, so a caller always gets something it can render.
- `--extensions=NAME[,NAME...]` — install Chrome Web Store extensions into every
  profile. Entries may be a known name (`odoo_debug`), a raw 32-character store id,
  or `name=id`, so a fork can install anything without editing the code.
  `--extensions=list` prints what is known and how to install anything else.
- Unpacked extensions can be vendored in `extensions/<name>/` and installed by
  directory name with **no network access**, editable in place (the source is
  re-copied into each profile on every launch). A local directory takes precedence
  over the same name in the Web Store table, so a store extension can be pinned or
  patched without renaming the commands that install it.
- `--flows-dir=DIR` and `--reports-dir=DIR`, so the scenarios can live in their own
  repository, separate from the engine.
- `--version` / `-V`.
- `users.example.json` — the config shape, with placeholder credentials.
- `pyproject.toml`: installable, with a `chrome-multi-session` console entry point and
  pinned upper bounds on dependencies.
- The engine's own fixture flows under `tests/fixtures/flows/`, so the compiler and
  loader tests no longer assert on any particular app's scenarios. One integration test
  compiles the real tree when one is present, and skips when it is not.

### Changed
- GUI: the tick boxes and radio dots are painted by a `QProxyStyle` rather than by the
  stylesheet, which can fill an indicator but cannot put a mark inside one — a ticked
  box used to be an empty accent square. This also reaches the check rows in the
  Accounts, Extensions and Scenarios lists, which the stylesheet never touched.
- GUI: the Sessions counter is a stepper built from real widgets, replacing Fusion's
  two stacked 7px arrows.
- `find_chrome()` prefers a candidate that answers `--version` over one that merely
  exists on `PATH`. Ubuntu 22.04's `chromium-browser` is a 2 KB shim that redirects to
  a snap: findable, executable, and not a browser. It also now looks in the Windows
  registry and Program Files, where Chrome is never on `PATH`.
- Chrome is started with a scrubbed `LD_LIBRARY_PATH` so that a packaged build's own
  bundled libraries are not forced on it.
- The auto-login extension's source moved out of a Python string literal into
  `extensions/_autologin/` as ordinary editable JS. Credentials and the login-form
  selectors are generated per profile into a `config.js` that the source reads, so
  nothing secret is in the checked-in files and the selectors are no longer hardcoded
  in JavaScript.

### Fixed
- Windows: profile folder names now drop the characters NTFS rejects, so an env like
  `localhost:8069` produces a usable directory. Linux and macOS names are unchanged —
  renaming them would orphan every existing logged-in session.
- Windows: `seed_password()` no longer writes a credential blob Chrome cannot decrypt
  there (its password store is AES-256-GCM under a DPAPI-wrapped key). It skips the
  step with a note; auto-login is unaffected, since that is the extension's job.
- `--init-users-json` wrote a config that then refused to load: it emitted
  `"tests": []`, and an empty list is rejected. The template is now generated with
  `json.dumps` (it had also been hand-quoted into invalid JSON) and omits the key
  entirely. A test now scaffolds a config and loads it.

### Changed
- **BREAKING**: every usable extension vendored in `extensions/` is installed by
  default, and a broken one is skipped with a warning instead of stopping the launch.
  `--extensions` overrides the default entirely — `none` installs nothing, `all` is
  the default stated explicitly. Previously the Odoo Debug extension was downloaded
  from the Web Store on first run unless `--no-odoo-debug` was passed; nothing is
  downloaded now unless a Web Store name or id is named explicitly.
- `--odoo-debug` / `--no-odoo-debug` are deprecated. Both still work, with a one-time
  notice, and will be removed in the next MAJOR.

### Removed
- The empty `extensions/` directory.

## [0.4.0]

### Added
- `--jobs=N|all`: drive several windows at once instead of one after another. Extras
  queue and start as a slot frees. Scenarios inside a window still run in order.
- `--run-tests=config`: each user runs its **own** `run-tests` field from `users.json`,
  so one command covers a whole role matrix.
- `--env=NAME`: select an environment by short name, matched against the config's `env`
  field, and supply that environment's URL — making `--url` optional.
- `select` action, for native `<select>` elements. Clicking an `<option>` silently does
  nothing, so this needed its own action rather than a selector change.
- `highlight` overlay component: flashes a box over each clicked / typed / pressed
  element. A bare `press` marks the focused element, which is otherwise invisible.
- The overlay tree now shows **every** scenario the window will run, keeping finished and
  failed ones on screen; the running one is expanded, the rest collapsed.

### Changed
- **BREAKING**: `--filter-prefix` removed; use `--env`.
- **BREAKING**: ad-hoc `--user` runs are keyed by environment + login rather than by
  login alone, so they no longer share one profile across environments — and they now
  share the profile a config-driven run uses. Old bare-login profile folders are unused.
- The `users.json` field `prefix` is now `env`. The old name is still read, with a
  one-time notice: accept the old form, warn once, remove in the next MAJOR.

### Fixed
- An empty `--filter-users=` / `--filter-prefix=` silently meant "all", so an unset shell
  variable launched every user in every environment. Empty values are now an error.
- Any unrecognised `-`-prefixed argument was treated as a positional URL, so a bare
  `--run-tests` launched Chrome on the literal string `--run-tests` and ran nothing.
- `--run-tests=config` joined each user's scenario ids into one string, and a string is a
  single id — the run failed to compile.
- The overlay's log bridge sat on the process-wide logger, so with several windows every
  HUD showed every window's logs, and one thread could drive another thread's Playwright
  page.
- `DevToolsActivePort` was never cleared, so a killed window left a stale port that the
  engine would attach to — or, if the OS had reassigned it, another window's browser.
