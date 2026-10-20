# Restrictions on a project folder

A program QAVector starts in a project folder - a service, or a step of a cycle -
normally runs with everything your account can reach: every file in your home
folder, the network, as much memory as the machine has. Restrictions hold it to
less, on the folder itself. Nothing has to be created first, and a folder that
sets none runs exactly as it always has.

Three things can be restricted, each on its own:

| | Values | What the program gets |
|---|---|---|
| **Network** | On / Isolated / Off | On: the host's own network - ports, local databases, the internet. Isolated: a network of its own - its ports cannot clash with anything, and it reaches only what is allowed (below). Off: nothing at all, not even this computer. |
| **Files** | No restrictions / Read and write / Read-only / No access | Read and write, Read-only and No access hold the program to the project folder (and the other folders listed). System folders stay readable so programs start; the rest of your home folder is hidden. |
| **Limits** | CPU cores, Memory, Processes | Empty means no limit. |

Paths stay the same inside: the project folder is at its own path, so commands,
working directories and config files written against it keep working, and the
program uses the same `python3`, `git` and libraries it always did.

## Where they are set

Restrictions belong to a path. Where there is no path - a cycle project that only
groups cycles, a services project without a folder - there are none, and nothing
changes.

**Services.** The project's **Project folder** has a restrictions button beside
it (Edit project): these are the defaults for every service in the project. Each
service's **Working directory** has the same button (Edit service): what is set
there replaces the project's defaults for that service only, in either direction
- a project can keep its services offline while the one that needs a database
port turns the network on for itself.

**Cycles.** **Properties → Project folder** takes a path or a path variable such
as `${vars.project_dir}`; the button beside it, and **Restrictions** in the
toolbar, open the cycle's restrictions - the defaults for its steps. Each step's
own window (double-click it) has a **Restrictions** section with the same button.

## An isolated network

Two services that both listen on 8069 cannot run side by side on the host's
network. Isolated gives each one a network of its own, so they can - and each
reaches only what it is allowed to:

- **Published ports.** A service's port put on this computer:
  `127.0.0.1:18069` leads to its `8069`. The port on this computer is a number
  or **auto** (a free one, taken when the service starts). The address is shown
  on the service's row while it runs; the row's menu opens it in the browser,
  copies it, unpublishes it or publishes one more, without restarting the
  service. "This computer" means 127.0.0.1 only; "Other computers" makes the
  port reachable from your network, asks first, and needs Internet on.
  Publishing adds no password or encryption.
- **Internet.** The service reaches the internet; names resolve through this
  computer's resolver.
- **Services on this computer.** The service reaches the ports listening on
  this computer's loopback - a database, another service's published port - at
  their usual `127.0.0.1` addresses, and the PostgreSQL socket folder.

Neither switch on, and nothing goes out at all: only the published ports come
in. A project can set Isolated and the two switches as defaults; ports are each
service's own. Isolated is for services for now; cycle steps get it together
with steps that publish and unpublish ports.

It runs on **pasta**, shipped with QAVector (GPL-2.0-or-later; see
[bundled-bubblewrap.md](bundled-bubblewrap.md)). A service that cannot have the
network it was given does not start: a port on this computer that is taken
stops it with the reason; only `auto` tries another.

## Quick toggles

Service rows, the project's header, cycle steps on the canvas and the cycles
toolbar show what is set now: **Network** (a globe; in a box when isolated, struck
through when off),
**Files** (folder, All/RW/RO/Off) and **Limits** (gauge). In the cycles toolbar the
globe stands alone and **Restrictions** beside it shows a lock: closed when
anything is restricted. They are indicators first; a click:

- on **On**, or on a folder that is open, switches that access off;
- on something this level switched off, turns it back on - to exactly what was
  configured, because switching off is stored beside the configuration
  (`switched_off`), not instead of it. The restrictions window shows it as off;
  choosing something else there replaces what was configured;
- on something off by the project's or cycle's default, turns it on for this
  service or step only.

Limits opens a small popover with the three numbers. Every change is a draft:
Save on the Services page (then restart a running service - its row says
"(edited)"), or the cycle's own Save, where Undo takes a toggle back. A change
not saved yet is drawn with a dashed edge.

## Which steps they hold

- **Held:** `command.shell`, `git.*`, `agent.*`, `task.brief`, `jira.*` and
  `teams.message`. Jira and Teams run inside QAVector itself, so when they are
  restricted the same plugin runs as a program of its own inside the sandbox;
  their tokens reach it on a pipe, never on disk.
- **Nothing to hold:** approvals, checks, choices, waiting, reports and memory
  touch neither the network nor the project. Their nodes show no indicators.
  `service.*` steps start a service the way the Services page does, with that
  service's own restrictions.
- **System steps:** `scenario.run` drives a real browser and cannot be held to a
  folder yet, and any step marked on purpose ("System step" in its window,
  `system: true` in the file). They run without restrictions, and the node says
  SYSTEM.

A restricted step runs in the project folder unless its `dir` says otherwise.

**A step that needs the network does not start without it.** An agent, Jira or
Teams step whose network is off fails at once, saying so and how to turn it on
- an agent's CLI left to try would retry for minutes and then fail with nothing
that names the cause. The node's tooltip says it beforehand.

## What a restricted program sees

- System folders (`/usr`, `/etc`, `/opt`, ...) read-only; its own empty `/tmp`,
  `/run` and home folder.
- The project folder as Files says, and the **other folders** listed in the
  window, each read-only or read-write (`~/` is your home).
- In a cycle, its own **run folder** (`${run.workspace}`), read-write, where
  steps hand files to each other. What QAVector keeps there - the run's record,
  graph and logs, and every step's output - is readable, so what went wrong can
  be worked out, but not rewritable. Other runs stay hidden.
- Folders a tool needs to start, added automatically and shown in the window: a
  virtualenv or CLI installed outside the project, `~/.claude` for the agent CLI
  (its `~/.claude.json` as a private copy), `~/.gitconfig`.
- With Network On, the DNS resolver and the PostgreSQL socket folder, read-only;
  isolated, only what its switches allow.
- With Network Off or Isolated, not this computer's local sockets - the session
  bus, a Docker socket, the X display - even with Files "No restrictions".
- Never QAVector's own data - secrets, saved logins, browser profiles, the cycle
  files and services.json - even inside a granted folder; `~/.ssh`, `~/.gnupg`,
  `~/.aws` stay hidden inside a granted folder unless granted themselves.

## Limits

Limits go through your session's service manager (`systemd-run --user --scope`):
it creates a cgroup, applies the limits before the program starts and removes it
when it ends. Memory and process limits work on a stock Ubuntu desktop. CPU
limits need the cpu controller delegated to user sessions, a one-time setting
for an administrator:

```bash
sudo mkdir -p /etc/systemd/system/user@.service.d
printf '[Service]\nDelegate=cpu cpuset io memory pids\n' | sudo tee /etc/systemd/system/user@.service.d/delegate.conf
sudo systemctl daemon-reload    # then log out and back in
```

Until then the window shows the reason and a CPU limit cannot be set.

## When restrictions cannot be enforced

Nothing set to be restricted ever runs unrestricted instead. A service whose
restrictions cannot be applied fails to start with the reason in its console; a
step fails with it. The usual reasons: no built runtime (a source checkout needs
`QAVECTOR_RUNTIME_DIR` pointing at `core/_internal/runtime` of a build, or a copy
in `/runtime/`), user namespaces disabled by the host, a limit the session cannot
enforce, a working directory outside what the program may see.

## The files

`services.json` (beside the project's `dir`, and in a service's entry):

```json
{"name": "Shop", "dir": "~/projects/shop",
 "restrictions": {"files": "read_write", "limits": {"memory_mb": 4096}},
 "runners": [
   {"name": "App server", "type": "python", "settings": {"script": "manage.py"}},
   {"name": "Tests", "type": "python", "settings": {"script": "-m", "args": "pytest"},
    "restrictions": {"network": "off"}}]}
```

A cycle:

```yaml
project_folder: ${vars.project_dir}
restrictions:
  files: read_write
  folders:
    - {path: ~/.cache/pip, access: read_only}
  limits: {memory_mb: 4096, processes: 256}
  switched_off: [network]     # written by the toolbar toggle
steps:
  - id: review
    plugin: agent.review
    restrictions: {files: read_only}
  - id: notes
    plugin: command.shell
    system: true
```

`network` is `on`, `isolated` or `off` (an unquoted `off` in YAML is read as off
too); isolated takes `outbound` and `host_loopback` (true or false) and, on a
service, `publish`: a list of `{internal_port, host_port (a number or auto),
host_ip (127.0.0.1), public}`;
`files` is `all`, `read_write`, `read_only` or `none`; `limits` takes
`cpu_cores`, `memory_mb` and `processes`.

## From the command line

```bash
qavector --restrictions-check                       # what this computer can enforce
echo '{"project_folder": "~/projects/shop", "layers": [["project", {"files": "read_only"}]],
       "command": ["python3", "app.py"]}' | qavector --restrictions-preview
qavector --restricted-exec='{"project_folder": "/abs/project", "layers": [["project", {"network": "off"}]]}' -- make test
# A running isolated service: list, add or remove a published port.
echo '{"state": "STATE.json", "op": "add", "publication": {"internal_port": 8069, "host_port": "auto"}}' \
    | qavector --network-ports
```

`--restricted-exec` takes `"network_state": "STATE.json"` for an isolated
service: the file says what it publishes while it runs, and goes when it ends.

## Not covered

An isolated network's Internet is all or nothing: there is no list of allowed
hosts yet - an agent cannot be given Claude's API alone - and Network On means
the host's network, local services included. Publishing is TCP; UDP and
`::1` are not published yet. Browsers (`scenario.run`) run as system
steps. Restrictions need Linux on x86_64; on
Windows the buttons say so. The stricter isolation from a reviewed file image
([Execution Runtime](QAVector_Execution_Runtime.md)) is configured with the
`--runtime-*` commands only; the GUI shows such a service or cycle as configured
from the command line and keeps it as it is.
