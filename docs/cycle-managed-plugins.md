# Installed plugins in Cycle

Package authors can start with the
[step-by-step build and verification guide](qavplug-authoring.md).

The Linux Cycle adapter executes installed `node_provider` contributions and
`agent_backend` roles through Plugin Host and the shared Execution Runtime.
Discovery reads registry metadata only. No installed module is imported by
the GUI, catalog, Cycle parser or executor process.

Built-in Cycle nodes remain part of QAVector and need no separate packages.
The bundled Claude adapter also ships with the application. This increment
does not extract every core node into an independently installed `.qavplug`.

## GUI

**Plugins** lives at the bottom of the sidebar, in the **QAVector** group, with
a puzzle-piece icon. It supports package inspection, explicit digest/publisher/
permission approval, inactive import, activation/rollback, disable, removal,
trust-key enrollment and static schema inspection. Import does not activate:
the user can stop before changing the active version. Retention guards and
compatibility errors come from the same manager used by the CLI.

The searchable catalog includes core-owned nodes, the bundled Claude backend and installed
packages, including on a fresh installation with no imported `.qavplug` files.
Core nodes display their existing metadata and cannot be activated, disabled or
removed through package actions. `--plugins catalog` exposes this unified view;
`--plugins list` continues to list registered packages only.

The inventory refreshes after lifecycle changes without restarting the GUI.
Unavailable installed versions remain visible with their state and compatibility
report. Source filters and per-entry labels distinguish built-in, bundled and imported entries;
managed lifecycle buttons do not remove or disable bundled code. After a core
upgrade, Activate can explicitly select the Claude version shipped in that core;
it cannot execute an older bundled version using newer code.

Entries show a category icon, description, source, publisher, version and status.
The details panel keeps lifecycle actions visible while its content scrolls;
on narrow windows it moves below the list. Compatibility and quarantine reasons
appear near the status. Incompatible/quarantined versions cannot be activated,
and an active imported version must be disabled before removal. Search also
matches contribution IDs. Selection survives refresh, and colors follow live
light/dark theme changes. Technical JSON remains in Details / schemas.

The import review has independently scrolling metadata and fixed consent/import
controls; missing runtime or compatibility approval keeps Import disabled.

Cycle Properties edits `agent_profiles` as JSON and can add a role by selecting
an installed backend. The agent node selects a role by ID. Installed ordinary
nodes expose JSON configuration, inputs and optional expected output schema.
Nested values retain their types; malformed JSON cannot be saved. Full custom
schema-generated forms remain a future UI enhancement; the Schemas button shows
the authoritative static schemas used at execution.

## Cycle declarations

Existing YAML is unchanged. External contributions require an approved runtime:

```yaml
id: plugin-example
execution:
  runtime:
    mode: isolated
    deployment: plugins
    revision: <approved-deployment-sha256>
    project_directory: /absolute/approved/project
steps:
  - id: metrics
    plugin: example.metrics.count
    with:
      plugin_version: '>=1.0.0,<2.0.0'
      config:
        threshold: 5
      inputs:
        values: [2, 8, 13]
```

The example's contribution must be installed and active. `plugin_version`
defaults to `>=0.0.0` for ordinary providers. Agent profiles use their existing
`backend_ref` and `backend_version` fields:

```yaml
agent_profiles:
  agent.business.analyst:
    backend_ref: example.agent.analysis
    backend_version: '>=1.0.0,<2.0.0'
    instructions: Analyze the supplied task.
    backend_options: {}
```

An `agent` step refers to this profile in `with.agent_profile` and supplies its
`task`. Role definitions are static; dynamic Cycle expressions belong in task
or ordinary node inputs/config. Managed agent `inputs`/`directory` are currently
rejected rather than silently ignored: workspace access comes from deployment,
and task/instructions carry textual context.

## Runtime and permission boundary

The deployment must pin the **exact base image** from the managed Python
descriptor shipped at `runtime/plugin-python.json`. Plugin Host verifies the
installed environment and adds its code plus the trusted bootstrap to that
base image. The final image, managed runtime, contribution and effective policy
are included in the execution fingerprints. A Python interpreter found in PATH
is never a substitute.

The Cycle transport intersects approved project mounts with the package's
workspace permission: `none` removes them; `read` makes them read-only; `write`
retains only access already permitted by the deployment. Other host mounts,
secrets, tool/integration grants and network access are removed. Runtime owns
the process tree; the worker also inherits a hard zero child-process limit.
Scoped `workspace.resolve` returns the runtime path and effective access, never
the host source path.

HTTPS routes, credential aliases and approved commands are available through
the [scoped Host API](plugin-services-v1.md), when package consent and a reviewed
`spec.plugin_services` deployment agree with effective policy. Commands run in
separate supervised offline Runtime jobs; the worker itself remains offline
without credential files or arbitrary child creation. The existing isolated Claude
gateway remains supported by its reviewed `agent.review` worker; it is not
silently routed through the generic stdio worker. Interactive input, remote
jobs, session resume and artifact publication are not negotiated here.

Each call has a finite wall-clock deadline (at most 300 seconds, further narrowed
by the node deadline and profile limit). Agent capabilities and hard limits are
validated by the SDK before provider execution. Results are accepted only after
Runtime termination and cleanup. Events and outcomes remain in the attempt
database under the node's operation directory. Unknown outcomes leave an
unresolved receipt and block automatic retry/Resume.

## Versions and Resume

New runs atomically select active package versions and persist exact version,
digest and contribution bindings in `.execution/plugin-lock.json`. Before each
call and on Resume, the host rechecks the original lock, package integrity,
runtime identity and QAVector/API compatibility. Updating the active version
does not change existing runs. Disabled versions can finish existing locked
runs; quarantine, incompatibility, missing files or a missing lock block them.
Referenced versions cannot be removed.

Completed successful calls can replay their saved receipt. A fully successful
Cycle has nothing to Resume; use Resume for failed or interrupted work, as with
existing built-in nodes. Completed dependencies remain pinned and retained.
Fully received HTTP responses followed by output-validation failure also replay
their saved outcome. Interrupted requests after send remain unresolved and block
automatic Resume, even if the plugin catches the service error.

## Verification and release scope

Tests cover metadata-only discovery, lifecycle/lock transitions, JSON/role UI,
native import and execution of both contribution kinds, update/disable/Resume,
quarantine, invalid config, cancellation, timeout and OS-enforced permissions.
`tests/test_plugin_frozen_cycle.py` additionally accepts `--runtime-frozen-core`
to exercise import and execution with Python/pip absent from PATH and an
isolated data directory. A real clean-machine/deb installation and the complete
supported OS matrix remain separate release acceptance work.
