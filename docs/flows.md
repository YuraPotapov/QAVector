# Writing flows

A **flow** is a YAML file describing browser steps. A **scenario** is a flow you can run
by name; a **block** is a flow other flows reuse with `use:`. They are the same format —
only where they live differs.

```
flows/
  selectors.yaml          # named selectors, shared by every flow
  scenarios/              # runnable by id:  --run-tests=my_scenario
    my_scenario.yaml
  auth/login.yaml         # a block, referenced as `auth.login`
  common/host_up.yaml     # a block, referenced as `common.host_up`
```

The id maps to the path: a **dotted** id `auth.login` is `auth/login.yaml`; a **bare** id
`my_scenario` is `scenarios/my_scenario.yaml`.

Point the engine at a different tree with `--flows-dir=DIR`, or the GUI's
**Settings -> Scenarios**.

**It becomes the only tree.** Nothing that ships with the application is searched behind
it, so the blocks and `selectors.yaml` your scenarios reference have to live in it too —
a folder holding one scenario that says `use: auth.login` will not compile. Copy what you
need across before pointing it at an empty folder:

```bash
cp -r flows/auth flows/common flows/selectors.yaml ~/my-flows/
```

A complete, runnable example lives in [`tests/fixtures/flows/`](../tests/fixtures/flows).

## A minimal scenario

```yaml
id: my_scenario
name: What this proves, in one line
tags: [smoke]
steps:
  - use: auth.login                 # reuse a block
  - goto: "{{env.origin}}/orders"
  - wait_for: list_view
  - fill:
      target: search_input
      value: "ACME"
  - press: Enter
  - assert_visible: first_list_row
```

`id` must match the filename. `name` and `tags` are optional.

## Step shapes

Two forms, interchangeable:

**Shorthand** — exactly one key, the action name:

```yaml
- click: submit_button
- press: Enter
- assert_visible: dashboard
```

**Verbose** — needed whenever you want `timeout:`, `retry:` or `state:`:

```yaml
- type: assert_not_visible
  target: error_dialog
  timeout: 2500
  retry: {attempts: 3, delay: 2}
```

A shorthand step with two keys is an error, so `- click: a` and `- fill: {...}` cannot be
accidentally merged into one step.

## Actions

| Action | Argument | Notes |
| --- | --- | --- |
| `goto` | url | `{{env.origin}}` is the usual prefix |
| `click` | selector | |
| `fill` | `{target, value}` | types into an input |
| `select` | `{target, value}` | native `<select>`; `value` is the **option value**, not its label — clicking an `<option>` does nothing |
| `press` | key | presses on whatever has **focus**, e.g. `Enter`, `ArrowDown` |
| `wait_for` | selector | waits for visible; `state:` can be `attached`, `hidden`, … |
| `use` | flow id | inlines another flow (see below) |

## Assertions

| Assertion | Argument | Passes when |
| --- | --- | --- |
| `assert_exists` | selector | the element is in the DOM (may be invisible — the only way to check an `<option>`) |
| `assert_visible` | selector | the element is visible |
| `assert_not_visible` | selector | the element does **not** appear within `timeout` |
| `assert_text_contains` | `{target, value}` | the element's text contains `value` |
| `assert_url_contains` | substring | |
| `assert_title` | string | |
| `assert_host_up` | url | HTTP reachability, no browser involved |

**`assert_not_visible` always waits its full timeout when it passes** — proving absence
means waiting the window out. It is usually where a slow scenario's time goes, so give it
a short explicit `timeout:` rather than the 30 s default.

## Services

A scenario can start, stop and restart a service from `services.json` — the ones on the
GUI's **Services & Logs** page — and wait until it is actually up.

```yaml
steps:
  - service_restart: "Storefront/Web"
  - type: wait_for_out
    target: "Storefront/Web"
    value: "HTTP service .+ running on .+:8069"
    timeout: 180000
  - goto: "{{env.origin}}"          # the window still holds the old process's page
  - use: auth.login
```

**Reload after a restart.** The browser is not restarted with the service, and the page it
already loaded is still on screen. Asserting on that DOM proves nothing about the backend
that just came back, so go to the server again — a `goto:` — before the assertions.

| Action | Argument | Meaning |
| --- | --- | --- |
| `service_start` | `Project/Service` | start it; returns as soon as the request is taken |
| `service_stop` | `Project/Service` | stop it |
| `service_restart` | `Project/Service` | stop, then start — dependencies and all |
| `wait_for_service` | `Project/Service` | wait until its **status** is running |
| `wait_for_out` | `{target, value}` | wait until its **output** matches the regex in `value` |
| `wait_for_criterion` | `{target, value}` | wait until the **criterion** named in `value` is lit |

**These need the GUI.** The services are owned by the process that started them — an
attached one is literally its child — so the launcher asks and the GUI acts. Run these
scenarios from the Scenarios page; from a bare terminal every service step fails at once
with `service steps need the GUI`, rather than pretending or hanging.

**Naming a service.** `Project/Service` as both appear on the page, split on the first `/`.
A bare `Web` works when exactly one project has a service by that name, and is an error
when two do — which the failure message says.

**`wait_for_out` is a regex**, `re.search` against each line, case-sensitive; open with
`(?i)` for insensitive. It will not compile the scenario if it will not compile as a regex.
A **blank** value is refused too, rather than treated as "match anything": an empty regex
matches the first line the service prints, so the step would pass instantly having proved
nothing. The same goes for an empty criterion name.
It sees **only what the service has printed since it last started**, so a `service_restart`
followed by a `wait_for_out` waits for the *new* boot's line and cannot be satisfied by the
previous one still sitting in the console — including in the window where the restart has
been accepted but the old process has not gone down yet, which is exactly when the step
after it runs.

**All three waits are about the run you asked for.** Once `service_start` or
`service_restart` has been accepted, nothing is answered from what the service said or was
before it: not a lit criterion, not a status that still says *running* because the old
process has not gone down yet. So `wait_for_criterion` behind a `service_restart` waits for
the new boot to light the tag, whether or not the service was already up when the scenario
started — the two cases behave the same, which is the whole point. If that restart ends in
a failure instead, the wait says so at once rather than sitting out its timeout.

**A criterion on a service this window adopted still answers.** A detached service that was
already running when the application opened printed its ready line before anything was
watching, and will not print it again; its criteria are filled in from that run's own log,
read from where the run began. Only its own output — a criterion that watches a file of its
own always reads that file, adopted or not.

**Status and criteria are different questions**, and the three waits keep them apart:
`wait_for_service` is about the process, the other two are about what it wrote. A service
whose port was already taken is *running* and has not printed its ready line; that is the
distinction, and it is usually the one you want. Use `wait_for_criterion` for something the
service is already configured to watch for, `wait_for_out` for a one-off.

**A restart is a stop and a start, in that order.** The service is signalled, the engine
waits for it to actually be down, and only then is it started again — so a scenario does
not have to sleep and hope. How long it is given before it is killed is the service's own
*Stop timeout* on the Services & Logs page: raise it for anything that must finish what it
is doing, because the default is eight seconds and a module update is not.

**Timeouts.** `service_start`/`stop`/`restart` default to **15 s** (they only wait for the
request to be taken, not for the service to be up); the three waits default to **120 s**,
because a cold backend building its assets routinely needs more than the engine's usual
30 s. `timeout:` overrides either. A wait that expires is a FAIL naming what it was still
waiting for; a service step that cannot be carried out at all is an ERROR.

### Running in the background

A scenario made only of service steps and `assert_host_up` can run with **no browser at
all**: RUN ▾ → **In Background** in the GUI, `--no-browser` on the command line. That is
the six actions in the table above plus the HTTP probe, counted after `use:` blocks are
expanded — `use: common.host_up` qualifies, `use: auth.login` does not. When anything
selected needs a page, the entry is greyed out and its tooltip names the scenario and the
page steps it takes; the launcher refuses such a run the same way, before it starts.

Nothing else about the run changes. Each selected account is a session, as always; how
many run at once, *All at once* and *Auto* — with the load governor raising and lowering
it as the run goes — work exactly as they do with windows, and so do Stop, stopping one
session, and the Run page. There are simply no windows: nothing to keep open, so the
launcher exits when the run ends, and nothing to photograph, so a report is `result.json`
plus any server log. It needs the GUI for the reason every service step does.

## Timeouts and retries

One default: **30 000 ms**, set once on the page when the engine attaches. Any step
without an explicit `timeout:` gets it.

```yaml
- type: assert_text_contains
  target: page_title
  value: "Order 1042"
  timeout: 5000
  retry: {attempts: 10, delay: 1}     # worst case 10 x 5s + 9 x 1s = 59s
```

Worst case for a step is `attempts x timeout + (attempts - 1) x delay`. There is **no
scenario-level or run-level timeout** — the only ceiling is the sum of the steps.

## Selectors

`target:` is looked up in `selectors.yaml` by name; anything not found is passed through
as a raw selector. So these are equivalent:

```yaml
- click: submit_button          # selectors.yaml: submit_button: "button[type=submit]"
- click: "button[type=submit]"
```

Prefer names: they are the seam that lets one tree target several app versions, and the
overlay shows the friendly name rather than raw CSS.

Selectors are **Playwright** selectors, not plain CSS — `:has-text()`, `:text-is()`,
`:visible` and `:nth-match()` all work. `:nth-match(.row, 2)` is how you express "there
is no second one":

```yaml
- assert_visible: ".group-header"                   # at least one
- type: assert_not_visible
  target: ":nth-match(.group-header, 2)"            # ...and no second -> exactly one
  timeout: 2500
```

**`dashboard` is a de-facto reserved name.** `auth.login` gates on it, and every scenario
starts with `use: auth.login`, so a new flows tree must define it as "the app is loaded
and signed in".

## Composition with `use:`

`use:` inlines another flow's steps at that point, recursively. Cycles are detected at
compile time.

```yaml
steps:
  - use: auth.login
  - use: orders.open_list
  - assert_visible: first_list_row
```

Blocks keep the readiness gate written once, and the overlay draws each `use:` as a
collapsible group.

## Parameters

`{{user.login}}` and `{{env.origin}}` are substituted at compile time from the run
context. `env.origin` is the environment's origin with no trailing slash; `env.url` is
the URL the window opened.

An unknown placeholder is an **error at compile time**, not a silent empty string — a
typo fails before the browser is touched.

## Tags

```yaml
tags: [smoke, role:agent]
```

Select by tag with `--run-tests=tag:smoke`. A tag selector is a **union**, not an
intersection: `tag:manual` selects everything tagged manual.

Three tags are excluded from `--run-tests=all`:

| Tag | Meaning |
| --- | --- |
| `template` | needs real selectors filled in before it can run |
| `manual` | has side effects (writes data) |
| `blocked` | parked on a known unmet dependency |

Naming a scenario by id runs it regardless of its tags — these only affect `all`.

## Adding a new action

The action vocabulary is deliberately small. Adding one touches **six** places, and
missing the first gives you "unknown action" at compile time while missing the second
gives a `ValueError` at run time:

1. `engine/compiler.py` — add the name to `SELECTOR_ONLY`, `SELECTOR_AND_VALUE`,
   `VALUE_ONLY` or `URL_TARGET`, according to its argument shape.
2. `engine/runner.py` — a branch in `_do_action`.
3. `adapters/base.py` — the method on `BrowserAdapter`. **Adding an `@abstractmethod`
   breaks any out-of-tree adapter**; prefer a concrete no-op default, as the overlay
   hooks do.
4. `adapters/playwright_adapter.py` — the implementation.
5. `engine/runner.py` — `_MARKED_ACTIONS` and `_mark_label`, if the overlay should flash
   a marker on the element it touches.
6. `tests/test_runner.py` — a dispatch test with the fake adapter.

Adding an **assertion** is smaller: a function in `engine/assertions.py`, an entry in its
`ASSERTIONS` dict, **and** the name in one of the compiler sets in step 1 — that last one
is easy to miss, and without it `_validate` rejects the assertion as an unknown action.

An action the **GUI** carries out rather than the browser skips steps 3–5 and adds two of
its own: an op in `engine/services.py` (which emits the request and waits for the answer)
and a branch in `gui/cms_gui/servicebridge.py` (which does the work and replies). The
compiler set is still step 1, and `SERVICE_TARGET` / `SERVICE_TARGET_AND_VALUE` are also
advertised through `--describe`, so the GUI's scenario editor needs them in both
`FALLBACK_ACTIONS` and `all_actions()` in `gui/cms_gui/pages/scenarios.py`.
