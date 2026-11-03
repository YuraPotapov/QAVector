# Universal `.qavplug` installer v1

For a complete author workflow with working code, wheel/archive build commands,
signatures and an isolated Cycle demo, see
[Building a `.qavplug` package, step by step](qavplug-authoring.md).

Development increment, 2026-10-31. The installer handles both `node_provider`
and `agent_backend` contributions, including multiple contributions in one
package. Other kinds require their own public extension points. Existing Cycle
YAML is unchanged. Import does not import any package module into core or GUI.

## CLI and approval

Commands are available through the installed core or the development module:

```sh
qavector --plugins inspect ./example.qavplug
qavector --plugins import ./example.qavplug --approval ./approval.json
qavector --plugins list
qavector --plugins catalog  # includes built-in nodes and the bundled backend
qavector --plugins show example.plugin 1.0.0
qavector --plugins check
qavector --plugins activate example.plugin 1.0.0
qavector --plugins rollback example.plugin 1.0.0
qavector --plugins disable example.plugin 1.0.0
qavector --plugins remove example.plugin 1.0.0
qavector --plugins diagnostics
qavector --plugins recover
# Development equivalent:
python -m plugin_host.cli --help
```

Global `--root PATH` and `--runtime DESCRIPTOR` precede the command. The default
store is `<user_data_root>/plugins`, outside frozen application resources.
`inspect` returns metadata, prior active version, signature status, requested
permissions, external prerequisites and an `approval_required` JSON document.
Review that document and save it as the `--approval` input. Approval binds the
exact archive digest, publisher identity and full requested grants. New versions
and changed permissions never inherit consent. Import defaults to inactive;
`--activate` requests activation in the installation transaction.

A signature is verified with an explicitly enrolled host key:

```sh
qavector --plugins trust-key publisher-key-id BASE64_ED25519_PUBLIC_KEY
```

`publisher` in a manifest is display text. Verified identity is the SHA-256 of
the enrolled key bytes. Unknown keys are rejected until explicitly enrolled;
invalid signatures are always rejected. Unsigned archives can be reviewed and
approved with identity `local:unsigned`; this records local acceptance and does
not authenticate an author. Official `qavector.*` IDs and bundled aliases cannot
be installed by external packages.

## QAVector release compatibility

Every installable package must declare its supported QAVector releases,
independently of its own version and public API compatibility:

```json
"compatibility": {
  "qavector": ">=0.16.48,<0.17.0",
  "plugin_api": ">=1.0.0,<2.0.0",
  "host_api": ">=1.0.0,<2.0.0",
  "agent_api": ">=1.0.0,<2.0.0"
}
```

`agent_api` is required only for agent contributions. The `qavector` interval
above is an example of a declared support window, not a promise that every
plugin works with every future 0.16 patch. Publishers choose and test their range.
Use `==0.16.48` for one release. Supported operators are `==`, `>=`, `>`, `<=`,
`<`; comma-separated comparisons are intersected. Only stable numeric
`major.minor.patch` versions are supported; no wildcards, caret, prereleases,
whitespace-separated ranges or inferred compatibility across major versions.

The same application version is used by the launcher and compatibility checks:
the checkout's `pyproject.toml`, the frozen build's `VERSION`, or installed wheel
metadata. An unknown/development/nonstable version blocks plugin execution.
No host Python distribution metadata can override a frozen build's own version.

Checks run before import/healthcheck, activation, resolution, managed worker
handoff and restoring run locks. `inspect` can show an incompatible package but
returns no approval template; `import` still rejects it. `list` keeps the stored
state and selection visible and adds fresh `compatibility`/`effective_state`
fields. After core upgrade/downgrade the report is recalculated without deleting
packages, resetting consent, choosing another plugin version or re-enabling
explicitly disabled packages. Resume rechecks the original pinned package; it
never substitutes a compatible active version.

`qavector --plugins check` reports current QAVector/API versions and per-package
required ranges, actual versions and reasons. It returns exit code 2 when any
installed version is incompatible. This is a release/API check and works without
a worker image; runtime ABI, trust, grants and prerequisites remain separate gates.
Old manifests without `compatibility.qavector` remain parseable for inspection
and history, but are incompatible for installation/execution until the publisher
releases a new package with an explicit range. Existing archive hashes and saved
run locks are never rewritten to retrofit that declaration.

## Archive and wheel rules

The ZIP root contains `manifest.json`, `checksums.json`, a JSON dependency lock,
`wheels/*.whl`, and referenced `schemas/*`. Optional files are `signature.json`,
`README.md`, `LICENSE`, `licenses/*` and `assets/*`. The existing manifest schema
is authoritative. The extension alone does not establish the format.

`checksums.json` is a JSON object mapping every regular file except itself and
`signature.json` to a lowercase SHA-256 hex digest. No missing or extra inventory
entries are allowed. The package digest is SHA-256 of the original archive bytes.
Repacking with different ZIP metadata produces a different package digest.

`signature.json` has exactly `algorithm: "Ed25519"`, `key_id` and a base64
`signature`. Signed bytes are `b"QAVPLUG-SIGNATURE-V1\n" + checksums_bytes`, with
one literal LF and the exact checksum document bytes. Payload hashes cover the
manifest and lock. Tests use a fixed Ed25519 seed and reject inventory changes.

The JSON file named by `dependencies.python_lock` has this layout:

```json
{
  "format_version": 1,
  "targets": {
    "linux-x86_64-cp310": [
      {
        "name": "example-plugin",
        "version": "1.0.0",
        "file": "wheels/example_plugin-1.0.0-py3-none-any.whl",
        "sha256": "<64 lowercase hex digits>"
      }
    ]
  }
}
```

Target names must match the manifest. The selected target needs the plugin's
own distribution and every transitive dependency. The SDK is supplied by the
pinned runtime and cannot be replaced by a wheel. Wheel names, versions, tags,
Python requirements, dependencies and RECORD hashes are checked as data, using
`packaging` for Python compatibility. The installer does not run pip, `setup.py`,
build hooks, scripts or executable package metadata.

This first installer supports ordinary root/purelib/platlib wheels. `.data`
`purelib`/`platlib` paths are relocated; scripts, headers and arbitrary data
install schemes are rejected explicitly. URL dependencies, dependency extras,
sdists, `.pth`, precompiled bytecode and interpreter startup customization are
unsupported. Console-script wrappers are not generated: contributions use
manifest entrypoints. Package dependencies on other plugins remain unsupported.
Schemas must be self-contained Draft 2020-12 documents with document-local
references; network and cross-file references are rejected.

ZIP validation covers both outer archives and selected wheels: absolute/traversal
paths, NUL/control bytes, backslashes, Windows reserved names, case/normalization
collisions, symlinks/special entries, encryption and file/directory collisions.
Files are copied as regular files into a new private directory; archive permission
bits and link metadata are never applied. Limits are 256 MiB compressed, 512 MiB
expanded, 128 MiB per file, 20,000 entries, 16 path components and ratio 200:1.
The combined selected wheel environment is also bounded to 512 MiB/20,000 files;
JSON metadata is bounded to 2 MiB. Only stored/deflated ZIP members are accepted.

Wheel layout and RECORD semantics follow the supported subset of the
[PyPA wheel specification](https://packaging.python.org/en/latest/specifications/binary-distribution-format/).

## Managed Python and startup isolation

Linux x86_64 is the current execution target. A release builder prepares a
separate relocatable Python/stdlib/SDK dependency bundle:

```sh
python -m examples.plugin_contracts.prepare_runtime \
  --output /new/path/plugin-runtime --busybox /usr/bin/busybox
```

This is build tooling, run with an explicitly reviewed interpreter. It performs
no downloads or plugin imports. `packaging/build_deb.sh` prepares and includes
this bundle; `QAVECTOR_PLUGIN_RUNTIME` supplies it to the core PyInstaller spec.
The resulting `runtime/plugin-python.json` pins every source by content hash.
Its relative source paths survive relocation. Runtime identity also covers
Python/ABI metadata and the trusted worker bootstrap. Installed applications
never invoke frozen `sys.executable -m pip` or search for system Python. Missing
bundled runtime or unsupported isolation blocks installation/activation.

Healthcheck runs only after approval, through Execution Runtime/Bubblewrap,
with no workspace mounts, network or credentials. The private temporary space
is limited to 16 MiB and wall time to 15 seconds. Before importing entrypoints,
the trusted bootstrap applies hard limits: 512 MiB address space, 5 CPU seconds,
zero new processes/threads, 64 file descriptors and 1 MiB per output file.
It checks importability/callability without constructing agents or contacting
providers. It is not a behavioral safety audit or a provider-connectivity test.

Worker startup uses `-I -S -B`, an explicit plugin path and the pinned SDK path.
Locked package dependencies take precedence over runtime SDK dependencies;
any incompatibility fails healthcheck/worker validation. No host site-packages,
user startup hooks or current workspace are searched. Payload files become
read-only, and Runtime mounts approved image contents read-only. State, cache,
logs and installation storage are separate.

External CLI requirements are shown during inspection. To activate such a
package, `--prerequisites FILE` supplies an exact map from requirement IDs to
`{"path":"/approved/tool","sha256":"..."}`. Each executable must already be
pinned as executable in the selected managed runtime image. No PATH lookup or
host login occurs. Agent authentication and provider-specific prerequisite
checks remain run preflight responsibilities.

## Transactions, retention and execution handoff

The registry writer transaction serializes installation, activation, rollback,
removal and run pinning across processes. A version is published only after
healthcheck. Files and parent directories are synchronized before registry
commit. Publication failures roll back registration and recovery removes orphan
staging/package/environment directories. An interrupted install cannot expose
an active partial version. Existing active versions remain available.

Same ID/version/digest is idempotent; a different digest conflicts. Publisher
and contribution ownership survive removal. Activation selects a verified
installed version atomically; rollback changes only this pointer, not effects
of earlier runs. Disable blocks new resolution while existing run locks retain
their exact version. Removal is blocked by active pointers, run locks and explicit
project/history references; diagnostics identify the blocking references.
There is no force-delete switch or automatic garbage collection of run history.

`PluginManager.worker_spec()` returns verified bindings, schemas, image, argv,
runtime identity, approved grants and prerequisite pins to the Runtime proxy.
`locked_worker_spec()` performs the same handoff against an existing run lock,
including retained disabled versions; quarantine and incompatibility still block.
It does not authorize execution. The caller must intersect grants with
Project/Cycle/Node policy and retain run pins. Package/kernel isolation does not
make malicious code harmless: granted files can be damaged, granted secrets
can be misused, and native code shares the host kernel boundary. A valid
signature proves provenance, not benign behavior.

## GUI/Cycle integration and remaining release acceptance

GUI Plugins controls and import → select → execute → Resume now support
managed Linux contributions of both kinds, including explicitly configured
[scoped HTTPS, credentials and commands](plugin-services-v1.md). See
[managed plugins in Cycle](cycle-managed-plugins.md) for JSON configuration,
role selection, exact runtime bindings and transport limits. Existing built-in
Cycle adapters remain available without separate installation. Windows worker
isolation is not enabled.

Version 0.16.50 passed frozen import/activation/Cycle execution and offline DEB
install/upgrade/remove/purge on clean Ubuntu 22.04 and Debian 12 containers; see
[the release evidence and limits](plugin-core-work-plan.md#follow-up-4-linux-release-build-and-installupdate-acceptance).
An expanded dependency license inventory, broader packaging matrix and
long-running retention/migration acceptance remain separate work. Core upgrades
must retain a matching managed runtime/SDK
for old pins; unavailable exact runtime content fails closed. No automatic
migration of plugin state or source rebuild is performed.
