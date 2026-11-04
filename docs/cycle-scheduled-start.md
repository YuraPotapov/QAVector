# Starting a cycle on a schedule

Status: **implemented in 0.16.56.** How to use it is in
[`docs/cycles.md`](cycles.md), *Starting on a schedule*. Two things were built
differently from the plan below - see *As built* at the end.

Before it, no cycle started by itself. A trigger (`watch:`) repeats a watchable
step's query, and the application asks a person about every new item ("Start
work on QA-123?"). A planned entry can start at a set time, but only once and at
most three days ahead. Nothing repeated a run every N seconds.

## The need

Some cycles should simply run on a schedule, with nobody asked. For example, a
production health check requests the site, checks the code, the time and the
body, and sends a desktop notification on failure. It should run every hour.

Making `command.shell` watchable does not give that. A listener offers new
*items* and waits for an answer, so the result would be an hourly "Start?"
window, not an hourly check.

## The shape

A second kind of trigger:

```yaml
triggers:
  - id: hourly
    start: auto      # new; the default is ask, today's listener
    every: 3600
```

A trigger with `start: auto` has no `watch` and no `pin`. When it falls due, the
cycle is started the way a person would start it, as an ordinary run with its
history and logs.

### Rules

- **Only cycles that are safe to repeat unattended.** The validator refuses
  `start: auto` in a cycle that has a step with `important: true` or an
  `agent.*` plugin. The same idea is used in the adaptive design, where
  `approve: policy` is accepted only for points that add read-only plugins.
- `every` is at least `WATCH_EVERY_MIN` (60 seconds), as today.
- **Only while the application is open**, like the listener. Runs missed while
  it was closed or the machine was asleep are not caught up.
- **The first run comes `every` seconds after the application opens**, not at
  once. Otherwise every restart of the GUI would start a run.
- **A run in progress makes it wait**, through the same `_due` queue planned
  starts use. Each trigger has at most one waiting entry, so a long release
  cannot pile up five hourly checks behind it.
- The global Listen switch pauses auto triggers too.

## What already exists

- Schedule: `every`, `last`, `due()`, `seconds_to_next()` and the timer in
  `Listener` (`gui/cms_gui/cyclewatch.py`).
- On/off and the status line: `Listener.set_enabled()`, `Listener.status()`.
- A queue of starts that fell due while something else ran: `_due` and
  `_start_due()` in `gui/cms_gui/main_window.py`, released by
  `_sync_cycle_block` and `_on_finished`.
- Starting a run: `MainWindow.start_cycle_run(cycle_id, variables=...)`.

Most of the work is connecting these.

## Changes

1. **Model.** `domain/cycle.py`, `cycle/model.py`:
   - Add `Trigger.start: str = "ask"`, with constants `START_ASK` and
     `START_AUTO`. Update the docstring ("One kind so far").
   - Add `start` to `TRIGGER_KEYS`. Read it in `_parse_triggers`, write it in
     `_trigger_document` (only when not `ask`), and expose it in `to_graph`.
   - Validation in `_trigger_problems` for `auto`:
     - `watch` and `pin` are not required;
     - an `auto` trigger that also has `watch` is refused, because it watches
       nothing;
     - an unknown `start` value is refused;
     - the cycle may have no `important` step and no `agent.*` step.
2. **Inventory.** `cycle/cyclefile.py`: add `start` to `row["triggers"]`, so the
   GUI knows which triggers start runs without opening each cycle.
3. **Watch core.** `cycle/watch.py`: `--cycle-watch` on an `auto` trigger raises
   a `WatchError` that says so, instead of failing on an empty `watch`.
4. **Run origin (optional, small).** `session_launcher.py`: add
   `--cycle-trigger=schedule:<id>` and pass it to `executor.run_cycle(trigger=...)`.
   Today that is always `MANUAL`. History can then tell a scheduled run from a
   manual one.
5. **Listener.** `gui/cms_gui/cyclewatch.py`:
   - `set_inventory` keeps `start` for each trigger;
   - `tick()` splits due triggers. `ask` goes through `WatchThread` as today.
     `auto` sets `last` and emits a new signal `run_due(cycle_id, trigger_id)`,
     without calling the core;
   - for `auto`, set `last` to the time the application opened, so the first
     run comes after `every`;
   - in `status()`, an auto trigger says "next run in 42:10".
6. **Main window.** `gui/cms_gui/main_window.py`:
   - connect `listener.run_due` to a new `_auto_due`, which appends to `_due`
     unless an entry for the same trigger is already waiting;
   - `_start_due` starts such an entry with `start_cycle_run(cycle_id)` and no
     question.
7. **Canvas.** In the first version, `enabled: false` in the file plus the
   global Listen switch are enough. The current switch sits in the watched
   step's settings, and an auto trigger has no step, so a toggle in the cycle
   toolbar comes later.
8. **Docs.** `docs/cycles.md`: add "Starting on a schedule" next to "Starting
   when a new task appears", with the safety rule and the "only while open"
   limit.

## Tests

- `tests/test_cycle_model.py`:
  - `start: auto` is parsed and round-trips through `to_document`;
  - an auto trigger without `watch` or `pin` is valid;
  - `auto` together with `watch` is refused;
  - `auto` in a cycle with an `important` or `agent.*` step is refused;
  - an unknown `start` value is refused.
- `tests/test_cycle_watch.py`: `check()` on an auto trigger raises `WatchError`.
- A `Listener` test with a fake clock:
  - a due auto trigger emits `run_due` and does not call `core.cycle_watch`;
  - nothing is emitted while disabled;
  - the first run comes only after `every`.

## Order and size

1. Model, validation and their tests (changes 1–3).
2. Listener, main window and the listener test (changes 5–6).
3. Run origin and docs (changes 4 and 8).
4. Try it on a health-check cycle with
   `triggers: [{id: hourly, start: auto, every: 3600}]`, using `every: 60` for
   the manual check.

Roughly 150 lines of code and 100 lines of tests.

## As built

- **Every project, whichever is open.** The plan connected auto triggers to
  `_due` and `start_cycle_run`, which only know the project on screen: a health
  check of one project would stop - or start against the wrong project's files -
  while somebody worked in another. Instead `gui/cms_gui/cycleauto.py` keeps the
  auto triggers of every project (`AutoStarts`, keyed by project; the one on
  screen from its inventory, the others from `--cycle-list` through a core aimed
  at that project, every ten minutes), and `ScheduledRuns` starts each due run
  in a launcher process of its own, built from the trigger's project's core:
  its cycles, secrets, memory, runs and browser profiles. Its run history entry
  and log go to that project.
- **Not through the window's run.** For the same reason, and because an hourly
  `start_cycle_run` would switch the page and reset what somebody is looking
  at, scheduled runs never use the window's launcher. One goes at a time, a
  trigger has at most one waiting, and a cycle the window is running waits
  until that run ends. A person's run is not held up by a scheduled one.
- **One more unattended rule.** Besides `important` and `agent.*`, a step that
  asks a person (`approval.gate`, `user.input`, `user.choice`) is refused too:
  with nobody there it fails every run. Disabled steps do not count.
- `--cycle-trigger=schedule:<id>` is refused unless the cycle has that auto
  trigger turned on, and cannot be combined with partial runs, Resume or an
  action.
- No global Listen switch existed to reuse; `AutoStarts.set_enabled` is there
  for one, and `enabled: false` in the file turns a trigger off.

