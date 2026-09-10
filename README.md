# chrome-multi-session

Open one isolated Chrome window per test user, each auto-logged in, each with its
own persisted session and a single saved credential in Chrome's password manager.
Reusable for any multi-user login setup.

## How it works

- The user list lives in a JSON config file (`users.json` by default). Each entry
  has an `env` (which environment it belongs to), a `class` (window class / profile
  label), a `login` and its own `password`.
- Every user gets a separate `--user-data-dir` Chrome profile, so all users can be
  logged in at once without sharing cookies.
- A tiny generated extension fills and submits the login form per profile. It
  submits once per tab: if the credentials are rejected and the login page comes
  back, it stops instead of resubmitting in a loop (it logs a warning to the tab's
  console).
- Each profile's password manager is seeded with that user's single login/password
  (Chrome runs with `--password-store=basic` so the value is decryptable).
- Sessions persist between runs; each launch opens a single clean tab.

Profiles and generated extensions live under `user_sessions/` next to the
script by default (git-ignored — they hold cookies and passwords); override the
location with `--sessions-dir=DIR`.

## Config file

`users.json` (next to the script; override with `--config=FILE`) is a JSON array,
one object per user. Each has a `login`, a `class` (window class / profile label),
a `password`, and an optional `env`:

```json
[
  {"env": "localhost:8069", "class": "Admin", "login": "admin", "password": "admin"},
  {"env": "localhost:8069", "class": "User",  "login": "user",  "password": "user"}
]
```

Each user's profile folder is `<env>-<login>` (e.g. `localhost:8069-admin`),
so the **`env`+`login` pair must be unique**. `--user-session=PREFIX` overrides
the folder prefix for the whole run. Path separators in an `env` (e.g. a URL) are
flattened for the folder name.

> `env` used to be called `prefix`. The old name is still read, so an existing
> config keeps working; rename it when convenient.

### Predefined tests per user

An entry can carry the scenarios that user should run, so a role never has to be
paired with its test by hand:

```json
{"env": "localhost:8069", "class": "Agent", "login": "role_agent",
 "password": "CHANGE-ME", "run-tests": ["access_agent"]}
```

`--run-tests=config` then gives **each window its own list**, instead of replaying
every scenario against every window — so one command covers the whole matrix:

```bash
python3 session_launcher.py --env=dev --run-tests=config --execution-overlay=all
```

The value takes a JSON list or a comma-separated string, and entries may be
scenario ids or `tag:NAME` selectors (singular `tag:`, one tag per entry — `tags:`
is rejected with a hint). `tests` is accepted as a shorthand for `run-tests`.

Because the field lives per entry, each environment can name its own variant — the
`localhost` rows use `access_division`, while the `app-dev`/`app-stg` rows use
the `access_division_dev` twin. A user with no `run-tests` still launches, but runs
nothing (logged as skipped); if *nobody* selected has one, the run stops before any
window opens.

Note `tag:` is a **union**: `tag:manual` selects every manual scenario, not the
intersection with another tag. Naming a scenario by id runs it regardless of its
tags — `manual` only keeps it out of `--run-tests=all`.

### Multiple environments

Give the same user set a different `env` per environment (the same login can
repeat across environments — the folder stays unique):

```json
[
  {"env": "localhost:8069",              "class": "Agent", "login": "role_agent", "password": "CHANGE-ME"},
  {"env": "https://app-dev.example.com/", "class": "Agent", "login": "role_agent", "password": "CHANGE-ME"},
  {"env": "https://app-stg.example.com/", "class": "Agent", "login": "role_agent", "password": "CHANGE-ME"}
]
```

Then launch one environment at a time with `--env`, which matches by short name
and **also supplies that environment's URL**:

```bash
# just the dev environment's users
python3 session_launcher.py --env=app-dev
```

The name is the host's first label (`app-dev`, `app-stg`, `localhost`), and
any unambiguous shortening works — `--env=dev`, `--env=stg`, `--env=local`. A name
that matches nothing, or several environments, exits with the full list rather
than launching something unintended.

Omitting `--env` launches every environment at once, which is rarely what you
want against a single URL — the script warns when a run spans more than one.

Only pass `--user-session` *without* `--env` at your peril: it forces one folder
prefix onto every entry and collapses the environments together (the script errors
if that collides).

It holds logins and passwords, so it's **git-ignored** — it isn't committed. Scaffold
a starter copy with:

```bash
python3 session_launcher.py --init-users-json
```

This writes a one-line example `users.json` (only if one doesn't already exist);
edit it to add your users.

## Install (Ubuntu 22.04)

For using the app rather than working on it. Nothing needs to be installed first —
no Python, no pip, no git — because the Python runtime, PySide6 and Playwright all
ship inside the package. Google Chrome is the one thing that does not, and the app
says so on startup if it cannot find one.

```bash
sudo apt install ./chrome_session_amd64.deb
```

Then launch **Chrome Multi Session** from the applications menu, or run
`chrome-multi-session` for the CLI.

Your sessions, credentials and reports live in `~/ChromeMultiSession`, separate
from the installed program, so installing a newer version over the top keeps all
of them.

## Install (Windows 10+)

There is no released Windows installer yet, but the build that makes one is in the
tree. On a Windows machine with Python 3.10+ and [Inno Setup
6](https://jrsoftware.org/isdl.php):

```powershell
powershell -ExecutionPolicy Bypass -File .\packaging\build_exe.ps1
```

That writes `installers\windows\<version>\chrome-multi-session-<version>-setup.exe`.
Run it, and **Chrome Multi Session** appears in the Start menu. It has to be built
on Windows - PyInstaller does not cross-compile.

Both builds, what lands where, and what to expect the first time the Windows one
runs: [packaging/README.md](packaging/README.md).

## Getting started (from source)

```bash
# 1. Chrome or Chromium must be on PATH
google-chrome --version

# 2. Environment + dependencies (Python 3.9+)
python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install -r requirements.txt

# 3. Your user list. Copy the example, or scaffold a one-user starter:
cp users.example.json users.json          # then edit it
#   ...or: python3 session_launcher.py --init-users-json

# 4. Launch
python3 session_launcher.py --env=localhost
```

That opens one Chrome window per configured user, each signed in. `users.json` holds
passwords and is git-ignored.

To run flows as well, install the extras and point `--run-tests` at a scenario:

```bash
python3 -m pip install -r requirements-dev.txt      # playwright, pyyaml, pytest
python3 session_launcher.py --env=localhost --run-tests=my_scenario --execution-overlay=all
```

**Writing your own flows: [docs/flows.md](docs/flows.md).** The GUI's Scenarios
page does the same thing without a text editor - it lists every scenario and
block, shows what each named target resolves to, and writes into
`~/ChromeMultiSession/flows`, which is searched before the tree that ships with
the app.

Somewhere else suits you better? Put the folder in the GUI's **Settings ->
Scenarios**, or run with `--flows-dir=DIR`. It then becomes the **only** tree the
engine reads, so copy the blocks and `selectors.yaml` your scenarios reference
across before pointing it at an empty folder — see
[docs/flows.md](docs/flows.md).

A scenario can also **start, stop and restart the services** on the *Services &
Logs* page, and wait until one is up - by its status, by a regex over its output,
or by one of its criteria. Those services belong to the GUI, so a scenario using
them is run from the Scenarios page rather than from a terminal; the *Services*
section of [docs/flows.md](docs/flows.md) has the six actions.

## Running the tests

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest tests/ -q
```

No browser is launched: the suite drives the engine against fake adapters and its own
fixture flows in `tests/fixtures/flows/`. One integration test compiles the real `flows/`
tree if there is one, and skips if there is not.

## The GUI

Everything below can also be driven from a desktop front-end that lives in
[gui/](gui/) — environments, a `users.json` editor, a builder for every flag on this
page, and a live view of a run (step tree, log, artifacts).

```bash
python3 gui/bootstrap.py     # creates gui/.venv, installs PySide6, starts the app
```

It is a *client* of this launcher, not a second implementation: it spawns
`session_launcher.py` through an interpreter you configure, asks `--describe` what
exists and follows `--events=-` for what happens. The two environments stay separate —
the GUI needs PySide6, the launcher needs playwright and cryptography. See
[gui/README.md](gui/README.md).

### `--describe` and `--events`

Both flags exist for programs rather than people, and are useful on their own:

```bash
# the whole inventory as JSON: environments, users (never passwords), scenarios
# with their tags, extensions, and the values each flag accepts
python3 session_launcher.py --describe

# a structured JSONL event per transition, on stdout - log records stay on stderr,
# so the two are clean, separate streams
python3 session_launcher.py --env=localhost --run-tests=smoke_odoo --events=- \
  1>events.jsonl 2>run.log
```

Event kinds: `launcher.start`, `window.launched`, `windows.ready`,
`session.attached` / `session.attach_failed`, `session.start`, `flow.start` (carrying
the same step tree the HUD draws), `step.start` / `step.end` / `step.retry`,
`flow.end`, `artifacts.written`, `serverlog.lines` (a batch of backend log lines
belonging to one window - see *Server logs, tied to a window*), `run.dir`,
`run.summary`, `window.exited`. Each line
has `ts`, a monotonic `seq`, `kind`, and — for anything inside a window — `session`.
`--events=PATH` appends to a file instead.

## Compatibility

This project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The
public API the version is a promise about:

1. **CLI flags** and their accepted values
2. **The `users.json` schema** — `env`, `class`, `login`, `password`, `run-tests`
3. **The YAML flow format** — see [docs/flows.md](docs/flows.md)
4. **The report artifacts** — `result.json` fields and the
   `reports/<run>/<session>/<scenario>/` layout
5. **The on-disk profile layout** — `<env>-<login>`. Changing it orphans real logged-in
   sessions on every user's machine, so it is a breaking change even though no signature
   moved.

Deprecations keep working for one MAJOR cycle with a one-time notice — as `prefix` →
`env` does today. While at `0.x`, breaking changes may land in a MINOR bump; see
[CHANGELOG.md](CHANGELOG.md).

## Requirements

- Google Chrome / Chromium on `PATH`
- Python 3.9+, `pip install -r requirements.txt` (the `cryptography` package)
- For `--run-tests` only: `playwright` and `pyyaml` (in `requirements-dev.txt`)

## Usage

```bash
python3 session_launcher.py --env=localhost
```

Runs in the foreground and holds every window — press **CTRL+C to close them all**
(logins persist for next time).

Options:

- `--env=NAME` — launch one environment, matched against the config's `env` field
  by short name (`--env=dev`). It also supplies that environment's URL, so `--url`
  is optional. Default: every environment.
- `--url=URL` — login URL (or pass it as a positional argument). Overrides the URL
  `--env` would supply.
- `--user-session=PREFIX` — prefix for this run's profile dir names, so separate
  sessions stay isolated (the per-user suffix is appended automatically).
- `--sessions-dir=DIR` — where to store profiles + generated extensions
  (default: `user_sessions/` next to the script).
- `--config=FILE` — path to the users JSON config (default: `users.json`).
- `--init-users-json` — write a starter `users.json` (at `--config`'s path if
  given) and exit; never overwrites an existing file.
- `--filter-users=LIST` — launch only a subset of the config, by login:
  `--filter-users=role_agent,role_manager`. Default is `--filter-users=all`
  (every user). Errors if a login isn't in the config, or if the value is empty.
- `--user=LOGIN` — launch a single user instead of the config list. Its password
  and window label come from the config; if the login isn't there, the
  environment's own shared password is used.
- `--password=PASS` — override the stored password. With `--user` it applies to
  that user (and needs no config entry at all); on its own it applies to every
  selected user.
- `--server-log[=LIST]` — stream the backend's own log next to the windows, each
  session shown only what was written after its window opened. `LIST` is
  comma-separated log names, `all`, `none`, `default` (what the bare flag means),
  or `list` to print what is configured. Under `--run-tests` it also keeps each
  scenario's slice in the report. See *Server logs, tied to a window*.
- `--server-log-show=NAME` — read one configured log and answer JSON: its lines,
  or the error. `--server-log-lines=N|all` says how much (default: the last 500).

**Everything `--env` provides is a default — any flag you pass explicitly wins.**

```bash
# one user, dev; password comes from users.json
python3 session_launcher.py --env=dev --filter-users=role_division

# a login that isn't in the config yet — borrows dev's shared password
python3 session_launcher.py --env=dev --user=role_agent3

# stale password in the config? override it for this run
python3 session_launcher.py --env=dev --user=role_agent3 --password=newpass

# a deep link, still using dev's users and profiles
python3 session_launcher.py --env=dev --url=https://dev.example.com/orders/12
```
- `--run-tests=LIST` — attach to each window over CDP, run scenarios, write reports.
  `LIST` is `all`, `config`, or a comma-separated list of scenario ids and
  `tag:NAME` selectors. `config` runs each user's **own** `run-tests` field from
  `users.json` (see below).
- `--detach` — fire-and-forget: leave windows running after the script exits.
- `--log-level=LEVEL` — `DEBUG`/`INFO`/`WARNING`/`ERROR` (default `INFO`; also via
  the `OPEN_USERS_LOG_LEVEL` env var).
- `--execution-overlay=LIST` — show the in-page execution HUD while running flows
  (`--run-tests`). `LIST` is a comma-separated set of components or `all`:
  `tree`, `progress`, `status`, `logs`, `highlight` (`notifications` is accepted
  but not drawn yet). Only valid together with `--run-tests`.

  The tree shows **every scenario the window will run**, not just the current one:
  finished ones keep their result (✔/✖) and stay on screen, the running one is
  expanded, the rest sit collapsed on a single line. So when scenario 1 fails and
  scenario 2 starts, what happened before is still there to look at.

  `highlight` flashes a box over the element each acting step touches — `click`,
  `fill`, `select` and `press` — captioned with the action (`click`,
  `fill 07-2026-0454`, `press Enter`). A bare `press` has no selector, so it marks
  whatever currently has **focus**, which is otherwise invisible in a screenshot.
  Assertions are not marked, or the page would strobe. The box is drawn over the
  element rather than styling it, so the page's own CSS is never modified, and it
  fades after ~1.4 s.
- `--extensions=LIST` — which extensions to install into every profile.

  **By default — with no flag — every usable extension in `extensions/` is installed.**
  One that is broken, or that this Chrome will not accept, is skipped with a warning
  naming the reason; the launch is never stopped by it.

  ```bash
  python3 session_launcher.py --env=localhost                    # everything in extensions/
  python3 session_launcher.py --env=localhost --extensions=none  # nothing
  python3 session_launcher.py --env=localhost --extensions=all   # the default, explicitly
  python3 session_launcher.py --env=localhost --extensions=odoo_debug   # exactly this one
  ```

  `--extensions` always wins over the default, `none` included. `LIST` entries may be
  local directory names, Chrome Web Store names or ids, or `name=id`.

  **See what is available:**

  ```bash
  python3 session_launcher.py --extensions=list
  ```

  **Ship your own, with no network at all** — drop an unpacked extension in
  `extensions/<name>/` (it needs a `manifest.json`) and use its directory name:

  ```bash
  python3 session_launcher.py --env=localhost --extensions=my_helper
  ```

  Nothing is downloaded, the source is re-copied into each profile on every launch so
  edits take effect immediately, and a local directory **wins over the same name** in
  the Web Store table — which is how you pin or patch a store extension without
  renaming the commands that use it. See [extensions/README.md](extensions/README.md).

  **Install a store extension that isn't named yet** — no code change needed. The id is the last
  path segment of the Chrome Web Store URL
  (`https://chromewebstore.google.com/detail/<slug>/`**`hmdmhilocobgohohpdpolmibjklfgkbi`**):

  ```bash
  python3 session_launcher.py --env=localhost \
    --extensions=fmkadmapgofadopljbjfkapdkoienihi

  # or name it inline - the name is used for the cache file and the log lines
  python3 session_launcher.py --env=localhost \
    --extensions=react_devtools=fmkadmapgofadopljbjfkapdkoienihi
  ```

  To make a name permanent, add one line to `KNOWN_EXTENSIONS` in
  `session_launcher.py`. Unknown names fail fast and print the list above rather than
  silently installing nothing.

  Each CRX is fetched once from the Web Store and cached under the sessions dir, then
  planted into the profile directly (Chrome 137+ blocks `--load-extension`). A fetch
  failure warns and the launch continues without that extension.

  **On Windows the install goes over CDP.** Planting the files and registering them
  in the profile's `Preferences` is not enough there: `extensions.settings` is a
  protected preference, Chrome checks every entry against an HMAC it wrote itself,
  and an entry added from outside is stripped on the first launch - silently, with
  the files still on disk. So Windows opens a loopback debug port for **every**
  launch (not just `--run-tests`) and asks Chrome to load the planted directories
  with `Extensions.loadUnpacked`, the same call Playwright and Puppeteer use, then
  reloads the tab so the auto-login content script runs on it. A window whose
  extensions cannot be installed still opens; the log says so, and auto-login is
  what is lost. Note the port is unauthenticated and local: any process on that
  machine can drive those signed-in browsers.

  Replaces `--odoo-debug` / `--no-odoo-debug`, which still work with a deprecation
  notice. Note the default changed: the Odoo Debug extension used to be installed
  unless you opted out, and now nothing is installed unless you ask.

- `--jobs=N|all` — drive **N windows at once** instead of one after another
  (default `1`). With more windows than jobs the extras queue and start the moment
  a slot frees. Scenarios *inside* a window always run in order — one page cannot
  be driven twice — so this only removes the wait between windows, and no flow
  changes meaning. Console lines gain a `[session]` tag while running in parallel.
  Only valid together with `--run-tests`.

  ```bash
  # the whole dev matrix at once instead of role after role
  python3 session_launcher.py --env=dev --run-tests=config --jobs=all
  ```

  **Parallelise windows, not data.** Read-only access matrices are safe. The
  write-flows mutate shared records in the app under test — and a fixture scenario
  other flows depend on must run first — so leave those at the default `--jobs=1`.
- `--close-after` — when running flows (`--run-tests`), close the browser windows
  automatically once the run finishes (report and screenshots written). Without it
  the windows stay open after execution so you can inspect the final app state (and
  the Execution Overlay, if enabled); close them manually or with CTRL+C. The
  launcher still exits with the aggregate run result either way. Only valid together
  with `--run-tests`.
- `--no-browser` — run the scenarios with no browser at all. Only for scenarios made
  of service steps and `assert_host_up`, their `use:` blocks included; any other is
  refused before the run starts, named with the page steps it takes. Sessions,
  `--jobs` and the load governor work exactly as they do with windows, and the
  launcher exits when the run ends. Service steps need the GUI, which is where this
  is offered: RUN ▾ → *In Background*. Only valid together with `--run-tests`.
- `--report-level=LIST` — which report artifacts to generate: `console`, `dom`,
  `result`, `screen`, `url`. Only the listed artifacts are produced, on success and
  on failure alike. Omit it to keep the default set (see below).
- `--report-always` — also produce a full report after a **successful** run, not
  only on failure. With `--report-level` the selected artifacts are used; otherwise
  the full default set is produced.
- `--report-screen=LIST` — when screenshots are captured: `start` (before the first
  step), `each` (after every step that passes), `finish` (when the flow ends,
  success or failure). Combine freely, e.g. `start,each,finish`. Has effect only
  when `screen` is among the report artifacts. All three report flags require
  `--run-tests`.

### Configurable reports

By default (no `--report-*` flag) each scenario writes `result.json` on success and
the full diagnostic bundle on failure — unchanged from before:

```
report/                       report/                  # on failure
    result.json                   console.log
                                  dom.html
                                  result.json
                                  screenshot.png
                                  url.txt
```

The three flags configure this independently: `--report-level` selects **what** is
generated, `--report-always` selects **when** a report is generated, and
`--report-screen` selects **when** screenshots are captured. Screenshots are named
`screenshot_start.png`, `screenshot_001.png`, `screenshot_002.png`, …,
`screenshot_finish.png` by capture point.

```bash
# result.json + a before/after screenshot, on both success and failure:
python3 session_launcher.py --user=admin --password=admin \
  --url=http://localhost:8069 --run-tests=my_scenario \
  --report-level=result,screen --report-always --report-screen=start,finish
```

An unknown artifact name or screenshot mode is a hard error; duplicate screenshot
modes are ignored; a `--report-screen` with no `screen` in `--report-level` is
ignored (with a warning).

### Server logs, tied to a window

Ten windows are open as ten different roles, one of them misbehaves, and the
server's log is a single stream with everyone's requests mixed together.
`--server-log` tails that stream and shows each session only the lines written
after **its own window opened** — live in the GUI's session panel, and, with
`server_log` in `--report-level`, in the report beside the screenshots.

It works with or without `--run-tests`, and it never opens a debug port: nothing
here talks to the browser.

**What is configured where.** `logsources.json` (beside `users.json`, git-ignored
because it names real hosts and can carry a token) has two levels, because one
machine usually serves several logs:

- a **connection** says *where* to run a reader — `local`, or `ssh` with
  host/user/identity/port;
- a **log** says *what* to read there — `file`, `docker`, `journal` or `http` —
  which environments it belongs to, and how to read its timestamps.

One connection serves every log on a machine, so a stand with three logs opens
**one** ssh connection, not three. Copy `logsources.example.json` to get started,
or edit it on the GUI's **Services & Logs** page — which also groups each log
under the stack it belongs to, and starts and stops the backends those logs come
from. That grouping is one optional `project` key per log, and rows the editor
creates also carry an `added` date so it can show the newest first. Both are the
GUI's own and are ignored by everything here; the CLI reads the file exactly as
it always has.

```json
{
  "connections": [
    {"name": "local", "type": "local"},
    {"name": "staging", "type": "ssh", "host": "staging.example.com",
     "user": "deploy", "identity": "~/.ssh/id_ed25519"}
  ],
  "logs": [
    {"name": "app", "connection": "local", "env": "localhost:8069",
     "type": "file", "path": "/var/log/odoo/odoo.log",
     "format": "odoo", "default": true},
    {"name": "app", "connection": "staging", "env": "https://staging.example.com/",
     "type": "docker", "container": "odoo", "format": "odoo", "default": true},
    {"name": "nginx", "connection": "staging", "env": "https://staging.example.com/",
     "type": "file", "path": "/var/log/nginx/error.log", "format": "nginx"}
  ]
}
```

`envs` must be spelled exactly as in `users.json` — that string is what ties a log
to the windows opened against it. A log **name** has to be unique within an
environment (so `--server-log=nginx` resolves to one thing), which is why `app` can
repeat across stands.

```bash
python3 session_launcher.py --env=staging --server-log            # the defaults
python3 session_launcher.py --env=staging --server-log=app,nginx  # by name
python3 session_launcher.py --env=staging --server-log=all
python3 session_launcher.py --server-log=list                     # what is configured
python3 session_launcher.py --server-log-show=nginx               # read it
python3 session_launcher.py --server-log-show=nginx --server-log-lines=all
python3 session_launcher.py --log-sources=~/ChromeMultiSession/logsources.json \
                           --server-log=list          # read it from elsewhere
```

`--server-log-show=NAME` reads a configured log and answers JSON — the last 500
lines by default, `--server-log-lines=N|all` for anything else. It is also how you
find out a connection works, and a better answer than a yes: an unknown host key or
a stopped container is survivable during a run (that log is marked unavailable and
the rest carry on), which is exactly what makes it easy to miss. The GUI's **Open
Tail** / **Open Full** buttons are this command.

**Starting the backends themselves is the GUI's job, not the CLI's.** The same page
also holds the *services* each project is made of — a Python script, a shell command,
a Docker container, a Compose file — with live status, a console, and a per-service
**Detach allowed** deciding whether it survives closing the window. Those live in `services.json`, under
`~/ChromeMultiSession` by default and settable in the GUI's **Settings**; copy
`services.example.json` to get started. The launcher neither reads that file nor
needs to: nothing about a run changed.

**In the report.** Under `--run-tests`, every scenario keeps the lines written
while *it* ran, one file per log — always, pass or fail, whatever `--report-*`
says:

```
reports/<run>/<session>/<scenario>/server_log-app.log
                                   server_log-nginx.log
```

Turning the streaming on is the whole decision: `server_log` is deliberately **not**
a `--report-level` value. Everything in that list is captured from the browser, and
a second switch for a backend's log could only ever create a way to stream it all
run and then throw the evidence away.

**Formats — any backend, not one.** `format` names the *shape* of a line, never an
application:

| Shape | Reads |
| --- | --- |
| `iso` | `2026-08-19T10:53:09.123Z`, `2026-08-19 10:53:09,123`, with or without an offset |
| `slash` | `2026/08/19 10:53:09` — nginx's error log, Go's `log` package |
| `clf` | `[19/Aug/2026:10:53:09 +0300]` — Common Log Format: Apache and nginx access logs |
| `syslog` | `Aug 19 10:53:09 host app[42]:` — rsyslog, and what `journalctl` prints |
| `none` | no timestamp in the line at all; each line is stamped as it arrives |

`iso` is also Python logging's default `%(asctime)s`, so that one shape already
reads Django, Flask, FastAPI/uvicorn, Celery, Gunicorn, Odoo, Node/pino, Rails and
`docker logs -t`. Those names all work too, as **aliases** for the shape they
write — `"format": "django"` reads better in a config file than `"iso"` does, and
means the same thing. `--server-log=list` and the GUI's picker show every accepted
value.

**`tz` is the setting to check first.** It says which clock the timestamps were
written by, for lines carrying no offset of their own — `local` (the default),
`utc`, or `+HH:MM`. Plenty of backends log UTC (Odoo among them); on a machine that
is not UTC, every line then parses hours into the past and matches no session's
window, so a log streams nothing at all. A tailed line was written moments ago, so
the reader notices when a timestamp disagrees with that by more than a couple of
minutes: it reads the line as written now and says once what is wrong and how to
fix it. Setting `tz` properly is still worth doing — the correction is a safety
net, not a substitute.

When no preset fits, give `timestamp` and `level` yourself and any backend works:
`regex` capturing the value in one group, `format` as a `strptime` pattern or the
literal `"iso"` / `"clf"`, and `tz` (`local` / `utc` / `+HH:MM`) for lines carrying
no offset. A line with no timestamp of its own inherits the previous line's, which
is what keeps a stack trace attached to the message that introduced it.

**Levels.** Whatever a backend calls it — `TRACE`, `NOTICE`, `WARN`, `SEVERE`,
`CRIT`, `FATAL`, `PANIC` — lands on one of five: `DEBUG`, `INFO`, `WARN`, `ERROR`,
`CRITICAL`. In the GUI they are coloured by severity (debug recedes, info green,
warn amber, error red, critical the same red with weight and a wash behind it), and
the filter is a threshold: picking `WARN` shows warnings *and worse*. A word nobody
recognises reads as `INFO` rather than crying wolf in colour.

```json
{"name": "worker", "connection": "staging", "env": "https://staging.example.com/",
 "type": "file", "path": "/var/log/myapp/worker.log",
 "timestamp": {"regex": "^\\[(\\d{2}/\\d{2}/\\d{4} \\d{2}:\\d{2}:\\d{2})\\]",
               "format": "%d/%m/%Y %H:%M:%S", "tz": "utc"},
 "level": {"regex": "\\b(TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\\b"}}
```

**Nothing about a log stops a launch.** A name that does not resolve, a config with
a mistake in it, a host that will not answer, a pattern that will not compile: each
is reported once and costs that one log. The windows open either way — a diagnostic
that prevents the thing being diagnosed is worse than no diagnostic.

> **What time-based correlation can and cannot do.** A session is shown the lines
> written after its window opened. Nothing inspects the request, so when several
> windows are open against the *same* environment they all see the same tail —
> time alone cannot say which of them made the call. It separates environments and
> separates runs; it does not separate two roles clicking at once.

### Execution overlay (HUD)

When running flows with `--run-tests`, add `--execution-overlay` to draw a live
Heads-Up Display **inside** the automated Chrome window — a real-time execution
tree, progress bar, status, and log stream. It is injected over the existing CDP
connection into an isolated Shadow DOM and never touches the app's own DOM; the
empty space around the card is click-through, so the page stays interactive.

The card itself is a small floating panel you can work with:

- **scroll** the tree / logs (the tree can get long),
- **collapse** any branch (click a group row) or all at once (the `⊟` button),
- **drag** it by its header to reposition it,
- **minimize** it (`–`), and
- toggle **click-through** (`▣`) — makes the whole card `pointer-events:none`
  again, so if it ever sits over an element the automation needs to click you can
  let the mouse pass straight through it (dragging/minimizing works too).

```bash
python3 session_launcher.py --user=admin --password=admin \
  --url=http://localhost:8069 --run-tests=my_scenario \
  --execution-overlay=tree,progress,status,logs
```

Single ad-hoc user:

```bash
python3 session_launcher.py --user=role_agent --password=CHANGE-ME \
  --url=http://localhost:8069/web/login
```

Full example (every option at once):

```bash
  python3 ./session_launcher.py \
    --env=localhost \
    --sessions-dir=./user_sessions \
    --config=./users.json \
    --filter-users=all
```

Example with user parameters:
```bash
  python3 ./session_launcher.py \
    --env=localhost \
    --sessions-dir=./user_sessions \
    --user='test' --password='test'
```

This launches one window per user from `users.json`, storing each profile under
`./user_sessions/localhost:8069-<user>`, and leaves the windows running after the
script exits (`--detach`).

Simplest possible run (all users from `users.json`, foreground, default dirs):

```bash
python3 ./session_launcher.py --env=localhost
```

## Windows

Create and activate the virtual environment, then install dependencies (PowerShell):

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Run — full example (PowerShell uses a backtick for line continuation):

```powershell
python .\session_launcher.py `
  --env=localhost `
  --sessions-dir=.\user_sessions `
  --config=.\users.json `
  --filter-users=all
```

## macOS

Create and activate the virtual environment, then install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

Run — full example:

```bash
python3 ./session_launcher.py \
  --env=localhost \
  --sessions-dir=./user_sessions \
  --config=./users.json \
  --filter-users=all
```

**Keychain prompt:** macOS Chrome encrypts saved passwords with a secret kept in the
login Keychain, so the first run shows a *"security wants to use your confidential
information"* dialog. Choose **Always Allow** — later runs are then silent. If you
dismiss it you'll see `Skipping password save … could not read the Chrome Safe
Storage key`; the windows still open and auto-login still works, only the entry in
Chrome's password manager is missing.


------------------------------------------


cd ~/chrome-multi-session

# dev — all 9 roles, read-only matrix (what you just verified piecewise):
./run_access_matrix.sh https://app-dev.example.com

# dev — plus the dev-safe writes (agent edit, manager edit/dictionary/team-roles, admin team-roles, export):
./run_access_matrix.sh https://app-dev.example.com dev --writes

# localhost with the 76SEED seed — originals incl. fin_both and dept/name scoping:
./run_access_matrix.sh http://localhost:8069

# localhost — absolutely everything, incl. the mutating state-dependent flows:
./run_access_matrix.sh http://localhost:8069 seeded --writes

------------- TESTING ROLES ------------------------------

cd <your checkout>

ENV=app-dev        # which environment: app-dev, app-stg or localhost
                     # (--env supplies its URL too, so no ORIGIN/PREFIX pair)

for R in admin manager agent fin_comp fin_fine fin_both division analyst analyst2; do
  echo "=== $R ==="
  python3 session_launcher.py \
    --env="$ENV" --filter-users="role_$R" \
    --run-tests="access_$R" \
    --execution-overlay=all --close-after
done

------ Each command ------

Same thing unrolled, one role per line, so a single role can be re-run on its own.
--env picks the ENVIRONMENT, --filter-users the LOGIN; together they select exactly
one session, which is what keeps the matrix honest (the engine runs every requested
scenario against every ATTACHED session, so two roles must never share a run).
--env also supplies the URL, so there is no origin to keep in sync by hand.
Each window stays open after its run so the overlay can be inspected: close it yourself,
or append --close-after to have the launcher do it.

ENV=app-dev        # or app-stg, or localhost (--env=dev/stg/local also work)

# --- read-only matrix (safe on shared environments) ---
python3 session_launcher.py --env="$ENV" --filter-users=role_admin    --run-tests=access_admin    --execution-overlay=all
python3 session_launcher.py --env="$ENV" --filter-users=role_manager  --run-tests=access_manager  --execution-overlay=all
python3 session_launcher.py --env="$ENV" --filter-users=role_agent    --run-tests=access_agent    --execution-overlay=all
python3 session_launcher.py --env="$ENV" --filter-users=role_fin_comp --run-tests=access_fin_comp --execution-overlay=all
python3 session_launcher.py --env="$ENV" --filter-users=role_fin_fine --run-tests=access_fin_fine --execution-overlay=all
python3 session_launcher.py --env="$ENV" --filter-users=role_fin_both --run-tests=access_fin_both --execution-overlay=all
python3 session_launcher.py --env="$ENV" --filter-users=role_division --run-tests=access_division --execution-overlay=all
python3 session_launcher.py --env="$ENV" --filter-users=role_analyst  --run-tests=access_analyst  --execution-overlay=all
python3 session_launcher.py --env="$ENV" --filter-users=role_analyst2 --run-tests=access_analyst2 --execution-overlay=all

# --- everything tagged for a role: read + write + extras ---
# tag:role:<r> is a UNION, not an intersection - it pulls in that role's WRITE scenarios
# too (role:manager = 7, incl. setup_manager_foreign_ticket and the Team Roles wizard).
# Use on localhost / a scratch DB; on a shared environment prefer the read-only list above.
python3 session_launcher.py --env="$ENV" --filter-users=role_admin    --run-tests=tag:role:admin    --execution-overlay=all
python3 session_launcher.py --env="$ENV" --filter-users=role_manager  --run-tests=tag:role:manager  --execution-overlay=all
python3 session_launcher.py --env="$ENV" --filter-users=role_agent    --run-tests=tag:role:agent    --execution-overlay=all
python3 session_launcher.py --env="$ENV" --filter-users=role_fin_comp --run-tests=tag:role:fin_comp --execution-overlay=all
python3 session_launcher.py --env="$ENV" --filter-users=role_fin_fine --run-tests=tag:role:fin_fine --execution-overlay=all
python3 session_launcher.py --env="$ENV" --filter-users=role_fin_both --run-tests=tag:role:fin_both --execution-overlay=all
python3 session_launcher.py --env="$ENV" --filter-users=role_division --run-tests=tag:role:division --execution-overlay=all
python3 session_launcher.py --env="$ENV" --filter-users=role_division --run-tests=access_division_dev --execution-overlay=all
python3 session_launcher.py --env="$ENV" --filter-users=role_analyst  --run-tests=tag:role:analyst  --execution-overlay=all
python3 session_launcher.py --env="$ENV" --filter-users=role_analyst2 --run-tests=tag:role:analyst2 --execution-overlay=all

# --- fixture: run ONCE as the manager before access_agent / write_agent_edit on any
#     environment without the 76SEED seed (e.g. app-dev) ---
python3 session_launcher.py --env="$ENV" --filter-users=role_manager  --run-tests=setup_manager_foreign_ticket --execution-overlay=all

# There is no role:agent2 - role_agent2 is only ever a TARGET user
# (see write_manager_team_roles), never a session a scenario runs as.

