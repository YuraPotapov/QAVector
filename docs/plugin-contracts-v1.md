# Plugin contracts v1 — development preview

Status: v1 development contracts, initially introduced 2026-10-30. The current
implementation includes [Plugin Host/workers](plugin-host-runtime-v1.md),
[archive installation](qavplug-installer-v1.md) and
[Cycle integration](cycle-managed-plugins.md). For a complete runnable package,
use the [step-by-step authoring guide](qavplug-authoring.md). Contracts remain
subject to revision until release conformance stabilizes.

## Universal packages and contribution-specific APIs

`.qavplug` is the package format for **all supported QAVector extensions**.
It is not an agent package format. A package owns one or more contributions.
Package validation, trust, permissions, environments, version storage and
lifecycle must not depend on whether those contributions use an agent.

The initial contribution kinds are `node_provider` and `agent_backend`.
An ordinary node can process data, invoke a tool or implement another Cycle
operation without a model, prompt, Agent Profile or Agent API dependency.
Agent backends additionally implement the Agent API. A single package may
contain both. Future kinds use the same container and lifecycle; core must
first expose their extension points. Unknown kinds are rejected explicitly,
not executed as a guessed type.

`qavector_sdk` imports no private core, GUI or provider implementation. Its
initial Python API contains:

| Contract | Purpose |
|---|---|
| `ExecutionContext` | Opaque runtime/tools/credentials/cancellation handles |
| `PluginError`, `PluginEvent` | Common wire error and event envelopes |
| `NodeProvider.execute` | Ordinary executable Cycle contribution |
| `AgentBackend.describe`, `.execute` | Effective agent capabilities and execution |
| `validate_contract` | Validate published wire contracts and manifest metadata |
| `validate_output`, `validate_schema` | Validate business output/config and schemas |
| `preflight` | Agent option, capability and profile limit checks |

Python signatures are async for the managed worker SDK. The existing synchronous
Cycle executor remains unchanged. The SDK is currently included in the core
wheel. The IPC server and bundled managed worker runtime are implemented;
a separately published SDK distribution remains follow-up work.

## JSON Schemas

The canonical machine-readable contracts are `qavector_sdk/schemas/*-v1.schema.json`.
Schemas are included in Python package data and the core PyInstaller spec.
`load_schema(name)` returns an independent copy. Their stable resource IDs use
`urn:qavector:schema:<name>:1`, independent of filesystem location.

- Shared: `plugin-common`, `plugin-error`, `execution-context`,
  `plugin-contribution`, `qavplug-manifest`.
- Ordinary nodes: `node-request`, `node-result`, `node-event`.
- Agents: `agent-profile`, `agent-request`, `agent-result`, `agent-event`,
  `agent-error`, `agent-capabilities`, `agent-requirements`.
- IPC/Host API: `plugin-rpc`, `plugin-initialize`, `plugin-execute`,
  `plugin-host-call`, `plugin-input-response`.

Wire documents specify `schema_version: "1.0.0"`; manifests specify
`format_version: 1`. Unknown fields and unsupported versions are rejected.
`agent-error` references the common plugin error contract.

JSON Schema dialect: Draft 2020-12. Validation uses `jsonschema` and a closed
`referencing.Registry`. It never retrieves schema URIs over HTTP or reads
`file:` references. `local_schemas` is an explicit map of resource IDs to
already loaded schema documents. The installed-package importer validates
self-contained schemas before passing them to the SDK. General SDK validation
alone does not impose transport limits; the worker and installer enforce their
own documented frame, event and archive bounds.

Timestamps require an explicit UTC offset or `Z`, seconds, and an optional
fractional part. Leap seconds are excluded in v1. The timestamp validator does
not depend on optional `jsonschema` format extras being installed.

Package/API version syntax is stable `major.minor.patch`. Range syntax is a
comma-separated intersection of `==`, `>=`, `<=`, `>` or `<` comparisons,
without whitespace, prereleases, wildcards or `!=`. Manifest validation checks
syntax; `qavector_sdk.versions` parses and compares ranges for Plugin Registry.
Python dependency versions will use Python specifiers separately.

## Execution semantics

Requests carry run, node and attempt IDs. References and handles are opaque:
validation never opens a workspace, artifact or credential reference. The host
must resolve ownership, permissions, existence and size before use.

Result status is terminal: `completed`, `failed`, `cancelled`, `timed_out` or
`unknown`. Execution completion is separate from the business verdict in
`output`. Non-completed results require a normalized error. `unknown` requires
`EXECUTION_STATE_UNKNOWN` and `retryable: false`. An error includes side-effect
status. The host must still reconcile ambiguous external execution and must
not infer retry safety from a plugin's claim alone.

An agent's `usage` is explicitly `null` when unavailable. Available metrics
include source, name, value and unit. No usage is synthesized for ordinary
nodes. Result metadata carries the contribution/backend ID and plugin version;
registry locks pin package digests, and attempt fingerprints cover the
profile and runtime policy. These records are connected to
[Cycle execution and Resume](cycle-plugin-pins.md).

Events have event/attempt IDs, a nonnegative sequence, timestamp, type and typed
payload. Terminal event result IDs and statuses must match the envelope.
`failed` can carry `failed`, `timed_out` or `unknown` results. The new host
persists and orders events, rejects conflicting duplicates/terminal outcomes,
and confirms local cancellation through Runtime cleanup.

Agent preflight checks explicit requirements and implicit workspace/session/
output needs. It checks closed backend option schemas, profile limit ceilings,
hard versus best-effort limits, tool visibility/binding, streaming, content
types and confirmed cancellation. It does not provide runtime isolation or
enforce a deadline. Effective capabilities must come from the backend's
`describe` in the selected runtime/model/configuration scope.

After execution, the host must validate the result envelope, bind it to the
active attempt, validate business output against the node schema and resolve
artifact references. `validate_output` reports `OUTPUT_SCHEMA_INVALID`;
invalid contract data reports `CONTRACT_INVALID`; backend configuration errors
report `CONFIG_INVALID`. These errors omit rejected values/schema text to
avoid inadvertently including credentials or task content.

## Non-agent example

`examples/plugin_contracts/node-manifest.json` declares `example.metrics.count`,
an ordinary node with separate config/input/output schemas. Its `CountProvider`
counts input values above a threshold. It uses no agent fields or dependencies.
The contract test validates the manifest and inputs, executes the example,
validates emitted events and checks the output.

These are source/metadata fixtures, **not installable `.qavplug` archives**.
They contain no wheels, lock inventory, checksums or signatures.

```python
from qavector_sdk import validate_contract, validate_output

validate_contract("node-request", request)
validate_contract("node-result", result)
validate_output(result["output"], request["output_schema"])
```

Run the portable tests with:

```sh
python -m pytest tests/test_plugin_contracts.py -q
```

## Contract boundary and follow-up work

Manifest validation alone is not archive validation, a trust decision,
permission authorization or installation approval. Installed execution uses
the separate installer, registry and Runtime gates. ZIP/wheel validation,
signatures, offline dependency installation, managed Python, scoped Host API
services and GUI/CLI import are implemented; see the linked installer and
Cycle documents for their supported scope. Interactive/remote sessions,
artifact publication, richer generated forms and broader platform/migration
acceptance remain follow-up work.


## Host release compatibility

The manifest's `compatibility.qavector` declares the supported QAVector release
range separately from `plugin_api`, `agent_api` and `host_api`. Its grammar uses
the same stable numeric comparisons as API ranges. It is mandatory at the host
installation/execution gate; the schema permits an omitted legacy declaration
only so inspection/history can report why the old package is unavailable.
See [installer compatibility](qavplug-installer-v1.md#qavector-release-compatibility).
