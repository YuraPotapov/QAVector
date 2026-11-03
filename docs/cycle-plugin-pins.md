# Cycle package pins

New Cycle runs using the built-in `agent` profile node or legacy Claude
review/edit/implement/merge resolve their backend through `PluginRegistry`.
The persistent catalog lives at `plugins/registry.sqlite3` under QAVector's
user data root. Catalog discovery reads bundled metadata/resources and never
imports a backend entrypoint.

Installed Node Providers and agent roles also use this plan when execution.runtime
is configured. Their archive/environment/runtime checks and permission boundary
are described in [managed plugins in Cycle](cycle-managed-plugins.md).

## What is locked

The bundled Claude manifest declares `qavector.agent.claude_cli`; the reserved
`claude_cli` alias resolves to that contribution. Registration attests bundled
ownership on the host. Manifest publisher text is not authority. Initial
registration activates the first version only; rediscovery does not reactivate
disabled packages or replace an active version selection.
After a core upgrade, explicitly activating the current bundled version through
Plugins or the CLI changes selection for new runs only; old exact pins are kept.

`cycle.plugin_plan.CyclePluginPlan` collects the graph's adapted backend
requirements before work begins. The registry atomically stores a unique run
lock. `.execution/plugin-lock.json` stores that exact lock and the step-to-
contribution mapping. `.execution/manifest.json` records per-step bindings:
plugin ID, version, digest and contribution metadata. The bindings contribute
to checkpoint fingerprints and new paid-call receipts. They are also checked
when borrowing results into a partial run.

The bundled digest is a deterministic inventory of Claude and SDK source,
schema, manifest and lock resources. It is not an archive hash or an assertion
that an external package is installed. Wheels and frozen distributions include
the inventory. Changing bundled content under an existing version is a conflict;
release builds must update the component version for changed content.
This identifies the adapter and SDK, not the separately installed Claude CLI,
its login state or the remote model. Pinning those execution prerequisites
belongs to the approved Runtime image/deployment.

## Resume and revocation

Resume requires the original registry lock and matching saved manifest. A
missing lock never triggers fresh resolution against the currently active
version. A lock is rechecked immediately before every backend invocation.
Quarantined or incompatible packages block existing runs. Disabled packages
cannot start new runs; already locked runs can finish, matching the registry's
existing lifecycle semantics.

Activating another version does not redirect a locked run. The compatibility
adapter can execute only the backend actually bundled in the current build:
an installed metadata row for another version is insufficient. If the exact
locked code is unavailable, execution stops rather than using a newer class.
Keeping multiple runnable versions requires the managed environments in work
item 4.

Historical runs without a package lock retain their original fingerprints and
receipt behavior. The system does not retroactively claim a package version for
them. A new run from the same unchanged YAML receives package pins. Non-agent
cycles do not create the plugin catalog. CrewAI/AutoGen retain their adapters. The isolated provider gateway uses the
shared backend through [its restricted transport](claude-gateway-backend-v1.md);
its exact source bindings come from the approved Runtime image and core resource
inventory, independently of the compatibility adapter's catalog locks.

## Remaining integration

This connects the currently shipped Claude adapter, not arbitrary external code
to host imports. Generic external backend/node execution, installation, managed
versioned environments and package removal/GC remain separate work items.
The bundled manifest's empty backend-specific dependency lock relies on the
SDK/runtime supplied by the trusted core distribution; it is not a complete
standalone `.qavplug` install payload.
