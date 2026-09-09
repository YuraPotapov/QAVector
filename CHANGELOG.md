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
